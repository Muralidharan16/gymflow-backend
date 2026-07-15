from __future__ import annotations

import re
import uuid
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.billing_parties import (
    BILLING_PARTY_CREATION_SOURCE,
    BILLING_PARTY_SYNTHETIC_PHASE,
    BILLING_PARTY_SYNTHETIC_PURPOSE,
    BillingPartyCreationCommand,
    BillingPartyCreationResult,
    FinanceBillingPartyError,
)
from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.repositories.billing_parties import FinanceBillingPartyRepository


IDEMPOTENCY_SCOPE = "finance.billing_party.create"
_TEST_WORDS = ("test", "sandbox", "dummy", "demo", "dev", "local", "mock", "staging", "qa")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
_PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_STATE_CODE_PATTERN = re.compile(r"^[0-9]{2}$")
_SAFE_METADATA_KEYS = {"test_mode", "purpose", "phase", "source"}
_PARTY_TYPES = {"individual", "business", "government"}
_GST_TREATMENTS = {"b2c", "b2b"}
_STATUSES = {"active"}


class FinanceBillingPartyCreationService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = FinanceBillingPartyRepository(session)

    async def create_billing_party(
        self,
        command: BillingPartyCreationCommand,
    ) -> BillingPartyCreationResult:
        normalized = _normalize_command(command)
        request_hash = canonical_hash(normalized)

        async with self._session.begin_nested():
            _validate_trusted_source(command)
            _validate_actor_scope(command)

            organization = await self._repo.get_organization(command.organization_id, for_update=True)
            if organization is None:
                raise FinanceBillingPartyError(
                    "BILLING_PARTY_ORGANIZATION_NOT_FOUND",
                    "Billing party organization was not found.",
                )
            if not organization.is_active:
                raise FinanceBillingPartyError(
                    "BILLING_PARTY_ORGANIZATION_INACTIVE",
                    "Billing party organization is not active.",
                )
            if command.synthetic_mode and not _is_test_organization(organization):
                raise FinanceBillingPartyError(
                    "BILLING_PARTY_SYNTHETIC_ORGANIZATION_REJECTED",
                    "Synthetic billing party creation requires an approved test organization.",
                )

            idem, idempotency_created = await self._repo.reserve_idempotency_key(
                organization_id=command.organization_id,
                scope=IDEMPOTENCY_SCOPE,
                idempotency_key=normalized["idempotency_key"],
                request_hash=request_hash,
            )
            await self._repo.acquire_organization_creation_lock(command.organization_id)

            existing = await self._repo.get_by_organization(command.organization_id, for_update=True)
            if existing is not None:
                _validate_existing_party(existing, normalized)
                if idempotency_created:
                    await self._repo.complete_idempotency_key(idem, response_ref=str(existing.id))
                elif not idem.response_ref:
                    raise FinanceBillingPartyError(
                        "BILLING_PARTY_IDEMPOTENCY_PROCESSING",
                        "Billing party request is already processing for this idempotency key.",
                    )
                return _result(existing, replayed=True)

            if not idempotency_created:
                if not idem.response_ref:
                    raise FinanceBillingPartyError(
                        "BILLING_PARTY_IDEMPOTENCY_PROCESSING",
                        "Billing party request is already processing for this idempotency key.",
                    )
                replayed = await self._repo.get_by_id(uuid.UUID(idem.response_ref))
                if replayed is None:
                    raise FinanceBillingPartyError(
                        "BILLING_PARTY_REPLAY_NOT_FOUND",
                        "Billing party idempotent response could not be found.",
                    )
                _validate_existing_party(replayed, normalized)
                return _result(replayed, replayed=True)

            party = await self._repo.create_billing_party(
                organization_id=command.organization_id,
                billing_name=normalized["billing_name"],
                party_type=normalized["party_type"],
                gst_treatment=normalized["gst_treatment"],
                billing_address=normalized["billing_address"],
                place_of_supply_state_code=normalized["place_of_supply_state_code"],
                status=normalized["status"],
                gstin=normalized["gstin"],
                pan=normalized["pan"],
                metadata_json=normalized["metadata"],
            )
            await self._repo.complete_idempotency_key(idem, response_ref=str(party.id))
            return _result(party, replayed=False)


def _normalize_command(command: BillingPartyCreationCommand) -> dict[str, Any]:
    if command.organization_id is None:
        raise FinanceBillingPartyError(
            "BILLING_PARTY_ORGANIZATION_REQUIRED",
            "Billing party organization is required.",
        )
    if command.actor_organization_id is None:
        raise FinanceBillingPartyError(
            "BILLING_PARTY_ACTOR_ORGANIZATION_REQUIRED",
            "Billing party actor organization is required.",
        )
    idempotency_key = _required_text(command.idempotency_key, "BILLING_PARTY_IDEMPOTENCY_INVALID", max_length=200)
    billing_name = _required_text(command.billing_name, "BILLING_PARTY_NAME_INVALID", max_length=200)
    billing_address = _required_text(command.billing_address, "BILLING_PARTY_ADDRESS_INVALID", max_length=500)
    party_type = _normalized_choice(command.party_type, _PARTY_TYPES, "BILLING_PARTY_TYPE_INVALID")
    gst_treatment = _normalized_choice(command.gst_treatment, _GST_TREATMENTS, "BILLING_PARTY_GST_TREATMENT_INVALID")
    status = _normalized_choice(command.status, _STATUSES, "BILLING_PARTY_STATUS_INVALID")
    state_code = str(command.place_of_supply_state_code or "").strip()
    if not _STATE_CODE_PATTERN.fullmatch(state_code):
        raise FinanceBillingPartyError(
            "BILLING_PARTY_STATE_CODE_INVALID",
            "Billing party place-of-supply state code is invalid.",
        )
    gstin = _normalize_optional_identifier(command.gstin, "BILLING_PARTY_GSTIN_INVALID", _GSTIN_PATTERN)
    pan = _normalize_optional_identifier(command.pan, "BILLING_PARTY_PAN_INVALID", _PAN_PATTERN)
    metadata = _normalize_metadata(command.metadata or {}, synthetic_mode=command.synthetic_mode)

    if gst_treatment == "b2b" and not gstin:
        raise FinanceBillingPartyError(
            "BILLING_PARTY_B2B_GSTIN_REQUIRED",
            "B2B billing parties require GST registration evidence.",
        )
    if command.synthetic_mode:
        if party_type != "individual" or gst_treatment != "b2c" or status != "active":
            raise FinanceBillingPartyError(
                "BILLING_PARTY_SYNTHETIC_SHAPE_INVALID",
                "Synthetic billing parties must use the approved B2C active shape.",
            )
        if state_code != "33":
            raise FinanceBillingPartyError(
                "BILLING_PARTY_SYNTHETIC_STATE_INVALID",
                "Synthetic billing party place of supply must be Tamil Nadu.",
            )
        if gstin or pan:
            raise FinanceBillingPartyError(
                "BILLING_PARTY_SYNTHETIC_TAX_ID_REJECTED",
                "Synthetic billing parties must not include GSTIN or PAN.",
            )
        if "test" not in billing_name.lower():
            raise FinanceBillingPartyError(
                "BILLING_PARTY_SYNTHETIC_NAME_INVALID",
                "Synthetic billing party name must be unmistakably test-only.",
            )

    return {
        "organization_id": str(command.organization_id),
        "billing_name": billing_name,
        "party_type": party_type,
        "gst_treatment": gst_treatment,
        "billing_address": billing_address,
        "place_of_supply_state_code": state_code,
        "gstin": gstin,
        "pan": pan,
        "status": status,
        "metadata": metadata,
        "synthetic_mode": bool(command.synthetic_mode),
        "source": command.source,
        "idempotency_key": idempotency_key,
    }


def _validate_trusted_source(command: BillingPartyCreationCommand) -> None:
    if command.source != BILLING_PARTY_CREATION_SOURCE:
        raise FinanceBillingPartyError(
            "BILLING_PARTY_SOURCE_REJECTED",
            "Billing party creation source is not trusted.",
        )


def _validate_actor_scope(command: BillingPartyCreationCommand) -> None:
    if command.actor_organization_id != command.organization_id:
        raise FinanceBillingPartyError(
            "BILLING_PARTY_CROSS_ORGANIZATION_REJECTED",
            "Billing party creation is not authorized for this organization.",
        )


def _validate_existing_party(existing, normalized: dict[str, Any]) -> None:
    existing_payload = {
        "organization_id": str(existing.organization_id),
        "billing_name": _collapse_text(existing.billing_name),
        "party_type": existing.party_type,
        "gst_treatment": existing.gst_treatment,
        "billing_address": _collapse_text(existing.billing_address),
        "place_of_supply_state_code": existing.place_of_supply_state_code,
        "gstin": existing.gstin,
        "pan": existing.pan,
        "status": existing.status,
        "metadata": _normalize_metadata(existing.metadata_json or {}, synthetic_mode=bool(normalized["synthetic_mode"])),
        "synthetic_mode": normalized["synthetic_mode"],
        "source": normalized["source"],
    }
    expected_payload = {key: normalized[key] for key in existing_payload}
    if canonical_hash(existing_payload) != canonical_hash(expected_payload):
        raise FinanceBillingPartyError(
            "BILLING_PARTY_DUPLICATE_CONFLICT",
            "An organization-bound billing party already exists with a different shape.",
        )


def _result(party, *, replayed: bool) -> BillingPartyCreationResult:
    return BillingPartyCreationResult(
        billing_party_id=party.id,
        organization_id=party.organization_id,
        billing_label=_safe_label(party.billing_name),
        party_type=party.party_type,
        gst_treatment=party.gst_treatment,
        status=party.status,
        replayed=replayed,
        synthetic_mode=bool((party.metadata_json or {}).get("test_mode")),
        metadata_summary={
            "test_mode": bool((party.metadata_json or {}).get("test_mode")),
            "purpose": (party.metadata_json or {}).get("purpose"),
            "phase": (party.metadata_json or {}).get("phase"),
        },
    )


def _required_text(value: str, code: str, *, max_length: int) -> str:
    normalized = _collapse_text(value)
    if not normalized or len(normalized) > max_length or _CONTROL_CHARS.search(str(value or "")):
        raise FinanceBillingPartyError(code, "Billing party command contains invalid text.")
    return normalized


def _collapse_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _normalized_choice(value: str, allowed: set[str], code: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise FinanceBillingPartyError(code, "Billing party command contains an unsupported value.")
    return normalized


def _normalize_optional_identifier(value: str | None, code: str, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if _CONTROL_CHARS.search(normalized) or not pattern.fullmatch(normalized):
        raise FinanceBillingPartyError(code, "Billing party tax identifier is invalid.")
    return normalized


def _normalize_metadata(metadata: Mapping[str, Any], *, synthetic_mode: bool) -> dict[str, object]:
    if not isinstance(metadata, Mapping):
        raise FinanceBillingPartyError("BILLING_PARTY_METADATA_INVALID", "Billing party metadata is invalid.")
    normalized: dict[str, object] = {}
    for key, value in metadata.items():
        if key not in _SAFE_METADATA_KEYS:
            raise FinanceBillingPartyError(
                "BILLING_PARTY_METADATA_INVALID",
                "Billing party metadata contains unsupported fields.",
            )
        if isinstance(value, str):
            if _CONTROL_CHARS.search(value) or len(value) > 80:
                raise FinanceBillingPartyError(
                    "BILLING_PARTY_METADATA_INVALID",
                    "Billing party metadata contains invalid values.",
                )
            normalized[key] = _collapse_text(value)
        elif isinstance(value, bool):
            normalized[key] = value
        else:
            raise FinanceBillingPartyError(
                "BILLING_PARTY_METADATA_INVALID",
                "Billing party metadata contains unsupported value types.",
            )
    if synthetic_mode:
        required = {
            "test_mode": True,
            "purpose": BILLING_PARTY_SYNTHETIC_PURPOSE,
            "phase": BILLING_PARTY_SYNTHETIC_PHASE,
        }
        for key, expected in required.items():
            if normalized.get(key) != expected:
                raise FinanceBillingPartyError(
                    "BILLING_PARTY_SYNTHETIC_METADATA_INVALID",
                    "Synthetic billing party metadata is not approved.",
                )
    return dict(sorted(normalized.items()))


def _is_test_organization(organization) -> bool:
    haystack = " ".join(
        str(value or "").lower()
        for value in (organization.name, organization.slug, organization.business_type, organization.description)
    )
    return any(word in haystack for word in _TEST_WORDS)


def _safe_label(value: str) -> str:
    normalized = _collapse_text(value)
    if len(normalized) <= 24:
        return normalized
    return f"{normalized[:21]}..."
