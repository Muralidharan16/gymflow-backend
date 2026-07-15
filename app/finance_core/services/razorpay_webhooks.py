from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.provider_boundary import (
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
    NormalizedProviderEventResult,
    ProviderSandboxConfig,
    validate_sandbox_provider_config,
)
from app.finance_core.domain.provider_capture_confirmation import ConfirmProviderPaymentEvidenceCommand
from app.finance_core.domain.razorpay_sandbox import (
    RazorpaySandboxConfig,
    validate_razorpay_sandbox_config,
    verify_razorpay_webhook_signature,
)
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput, RazorpayWebhookPaymentReference
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.provider_capture_confirmation import FinanceProviderCaptureConfirmationService


RAZORPAY_EVENT_STATUS_MAP = {
    "payment.authorized": "authorized",
    "payment.captured": "captured",
    "payment.failed": "failed",
    "order.paid": "captured",
}
_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_PROVIDER_REF_PATTERN = re.compile(r"^[A-Za-z0-9_:-]{1,200}$")


class RazorpayWebhookConfirmationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        razorpay_config: RazorpaySandboxConfig,
        provider_config: ProviderSandboxConfig,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._razorpay_config = validate_razorpay_sandbox_config(razorpay_config)
        self._provider_config = validate_sandbox_provider_config(provider_config)
        self._guard_service = guard_service or FinanceOperationalGuardService()
        self._confirmation = FinanceProviderCaptureConfirmationService(
            session,
            provider_code=self._provider_config.provider_code,
        )

    async def confirm_payment_event(self, webhook: RazorpayWebhookInput) -> NormalizedProviderEventResult:
        reference = self.normalize(webhook)
        self._guard_service.require_safe_preflight()
        result = await self._confirmation.confirm_provider_evidence(
            ConfirmProviderPaymentEvidenceCommand(
                provider_code=self._provider_config.provider_code,
                provider_event_id=reference.provider_event_id,
                event_type=reference.event_type,
                provider_order_ref=reference.provider_order_id,
                provider_payment_ref=reference.provider_payment_id,
                provider_amount_subunits=reference.provider_amount_subunits,
                provider_currency=reference.provider_currency,
                provider_payment_status=reference.provider_payment_status,
                provider_captured=reference.provider_captured,
                provider_payment_order_ref=reference.provider_payment_order_id,
                provider_order_entity_ref=reference.provider_order_entity_id,
                provider_order_status=reference.provider_order_status,
                provider_event_timestamp=reference.provider_event_timestamp,
                idempotency_key=webhook.idempotency_key or f"razorpay:{reference.provider_event_id}",
                webhook_signature_verified=reference.signature_verified,
            )
        )
        return NormalizedProviderEventResult(
            payment_event_id=result.payment_event_id,
            provider_code=result.provider_code,
            provider_event_id=result.provider_event_id,
            event_type=result.event_type,
            raw_status=reference.mapped_status,
            payment_id=result.payment_id,
            payment_status=result.payment_status,
            state_applied=result.state_changed,
            state_ignored=result.state_ignored,
            replayed=result.replayed,
        )

    def normalize(self, webhook: RazorpayWebhookInput) -> RazorpayWebhookPaymentReference:
        if not webhook.signature:
            raise FinanceWebhookSignatureError("Missing Razorpay webhook signature")
        if not webhook.raw_body:
            raise FinanceWebhookNormalizationError("Missing Razorpay webhook raw body")
        if not verify_razorpay_webhook_signature(
            raw_body=webhook.raw_body,
            signature=webhook.signature,
            webhook_secret=self._razorpay_config.webhook_secret,
        ):
            raise FinanceWebhookSignatureError("Invalid Razorpay webhook signature")

        provider_event_id = _authoritative_event_id(webhook.provider_event_id)
        try:
            payload = json.loads(webhook.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinanceWebhookNormalizationError("Invalid Razorpay webhook JSON") from exc
        if not isinstance(payload, dict):
            raise FinanceWebhookNormalizationError("Invalid Razorpay webhook JSON")

        event_type = _required_text(payload, "event")
        if event_type not in RAZORPAY_EVENT_STATUS_MAP:
            raise FinanceWebhookNormalizationError("Unsupported Razorpay webhook event type")
        payment_entity = _payment_entity(payload)
        provider_payment_id = _required_text(payment_entity, "id")
        provider_order_id = _required_text(payment_entity, "order_id")
        provider_amount_subunits = _required_nonnegative_int(payment_entity, "amount")
        provider_currency = _required_text(payment_entity, "currency")
        provider_payment_status = _required_text(payment_entity, "status")
        provider_captured = _optional_bool(payment_entity, "captured")
        provider_event_timestamp = _optional_nonnegative_int(payload, "created_at")

        provider_order_entity_id = None
        provider_order_status = None
        if event_type == "order.paid":
            order_entity = _order_entity(payload)
            provider_order_entity_id = _required_text(order_entity, "id")
            provider_order_status = _required_text(order_entity, "status")
            _validate_order_paid_payment_evidence(
                payload,
                canonical_payment_id=provider_payment_id,
                canonical_order_id=provider_order_entity_id,
            )

        return RazorpayWebhookPaymentReference(
            provider_event_id=provider_event_id,
            event_type=event_type,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            provider_amount_subunits=provider_amount_subunits,
            provider_currency=provider_currency,
            provider_payment_status=provider_payment_status,
            provider_captured=provider_captured,
            provider_payment_order_id=provider_order_id,
            provider_order_entity_id=provider_order_entity_id,
            provider_order_status=provider_order_status,
            provider_event_timestamp=provider_event_timestamp,
            mapped_status=RAZORPAY_EVENT_STATUS_MAP[event_type],
            signature_verified=True,
        )


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return _nested_entity(payload, "payment", "Razorpay event is missing payment entity")


def _order_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return _nested_entity(payload, "order", "Razorpay order.paid event is missing order entity")


def _nested_entity(payload: dict[str, Any], key: str, message: str) -> dict[str, Any]:
    root = payload.get("payload")
    container = root.get(key) if isinstance(root, dict) else None
    entity = container.get("entity") if isinstance(container, dict) else None
    if not isinstance(entity, dict):
        raise FinanceWebhookNormalizationError(message)
    return entity


def _validate_order_paid_payment_evidence(
    payload: dict[str, Any],
    *,
    canonical_payment_id: str,
    canonical_order_id: str,
) -> None:
    root = payload.get("payload")
    if not isinstance(root, dict):
        raise FinanceWebhookNormalizationError("Razorpay order.paid payload is invalid")

    payment_ids: set[str] = set()
    canonical_entity = _payment_entity(payload)
    payment_ids.add(
        _payment_id_from_order_paid_entity(
            canonical_entity,
            canonical_order_id=canonical_order_id,
        )
    )

    if "payment_id" in root:
        payment_ids.add(_required_provider_ref(root, "payment_id"))

    if "payment_ids" in root:
        values = root["payment_ids"]
        if not isinstance(values, list) or not values:
            raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")
        for value in values:
            payment_ids.add(_normalize_provider_ref_value(value, "payment_id"))

    payments_container = root.get("payments")
    if payments_container is not None:
        for entity in _supported_payment_entities(payments_container):
            payment_ids.add(
                _payment_id_from_order_paid_entity(
                    entity,
                    canonical_order_id=canonical_order_id,
                )
            )

    if payment_ids != {canonical_payment_id}:
        raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is ambiguous")


def _supported_payment_entities(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, dict):
        raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")

    entities: list[dict[str, Any]] = []
    if "entity" in container:
        entity = container["entity"]
        if not isinstance(entity, dict):
            raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")
        entities.append(entity)

    for key in ("items", "entities"):
        if key not in container:
            continue
        values = container[key]
        if not isinstance(values, list) or not values:
            raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")
        for value in values:
            if not isinstance(value, dict):
                raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")
            entities.append(value)

    if not entities:
        raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is invalid")
    return entities


def _payment_id_from_order_paid_entity(entity: dict[str, Any], *, canonical_order_id: str) -> str:
    payment_id = _required_provider_ref(entity, "id")
    payment_order_id = _required_provider_ref(entity, "order_id")
    if payment_order_id != canonical_order_id:
        raise FinanceWebhookNormalizationError("Razorpay order.paid payment evidence is inconsistent")
    return payment_id


def _authoritative_event_id(value: str | None) -> str:
    if not isinstance(value, str) or not _EVENT_ID_PATTERN.fullmatch(value):
        raise FinanceWebhookNormalizationError(
            "X-Razorpay-Event-Id is required and must use the supported format"
        )
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FinanceWebhookNormalizationError(f"Razorpay {key} is required")
    return value.strip()


def _required_provider_ref(payload: dict[str, Any], key: str) -> str:
    return _normalize_provider_ref_value(payload.get(key), key)


def _normalize_provider_ref_value(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinanceWebhookNormalizationError(f"Razorpay {key} is required")
    normalized = value.strip()
    if not _PROVIDER_REF_PATTERN.fullmatch(normalized):
        raise FinanceWebhookNormalizationError(f"Razorpay {key} uses an unsupported format")
    return normalized


def _required_nonnegative_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinanceWebhookNormalizationError(f"Razorpay {key} is required and must be a non-negative integer")
    return value


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise FinanceWebhookNormalizationError(f"Razorpay {key} must be a boolean")
    return value


def _optional_nonnegative_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinanceWebhookNormalizationError(f"Razorpay {key} must be a non-negative integer")
    return value
