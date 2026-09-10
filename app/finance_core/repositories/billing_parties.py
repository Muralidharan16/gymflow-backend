from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.billing_parties import FinanceBillingPartyError
from app.finance_core.models.foundation import FinanceBillingParty, FinanceIdempotencyKey


@dataclass(frozen=True)
class FinanceBillingPartyOrganization:
    id: uuid.UUID
    is_active: bool
    synthetic_billing_party_allowed: bool


class FinanceBillingPartyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_organization(
        self,
        organization_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FinanceBillingPartyOrganization | None:
        """Resolve billing-party organization authority through app_secure.

        The Finance runtime must not directly browse ``public.organizations``.
        When serialization is requested, take the bounded transaction-scoped
        advisory lock used for organization-bound billing-party creation, then
        call the app-runtime capability that validates the trusted tenant
        context and returns only the fields this service consumes.
        """
        if for_update:
            await self.acquire_organization_creation_lock(organization_id)
        result = await self._session.execute(
            text(
                """
                SELECT organization_id, is_active, synthetic_billing_party_allowed
                FROM app_secure.resolve_finance_billing_party_organization(:organization_id)
                """
            ),
            {"organization_id": organization_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return FinanceBillingPartyOrganization(
            id=row["organization_id"],
            is_active=row["is_active"],
            synthetic_billing_party_allowed=row["synthetic_billing_party_allowed"],
        )

    async def acquire_organization_creation_lock(
        self, organization_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"finance:billing_party:{organization_id}"},
        )

    async def reserve_idempotency_key(
        self,
        *,
        organization_id: uuid.UUID,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[FinanceIdempotencyKey, bool]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        result = await self._session.execute(
            text(
                """
                SELECT id, organization_id, scope, idempotency_key, request_hash_sha256,
                       status, response_ref, created_at, expires_at, inserted
                FROM app_secure.reserve_finance_idempotency(
                    :scope, :idempotency_key, :request_hash, :organization_id, :expires_at
                )
                """
            ),
            {
                "scope": scope,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "organization_id": organization_id,
                "expires_at": expires_at,
            },
        )
        row = result.mappings().one()
        key = FinanceIdempotencyKey(
            id=row["id"],
            organization_id=row["organization_id"],
            scope=row["scope"],
            idempotency_key=row["idempotency_key"],
            request_hash_sha256=row["request_hash_sha256"],
            status=row["status"],
            response_ref=row["response_ref"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )
        return key, bool(row["inserted"])

    async def complete_idempotency_key(
        self,
        key: FinanceIdempotencyKey,
        *,
        response_ref: str,
    ) -> None:
        result = await self._session.execute(
            text(
                """
                SELECT id, organization_id, scope, idempotency_key, request_hash_sha256,
                       status, response_ref, created_at, expires_at
                FROM app_secure.complete_finance_idempotency(:idempotency_id, :response_ref)
                """
            ),
            {"idempotency_id": key.id, "response_ref": response_ref},
        )
        row = result.mappings().one()
        key.status = row["status"]
        key.response_ref = row["response_ref"]

    async def get_by_organization(
        self,
        organization_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FinanceBillingParty | None:
        statement = select(FinanceBillingParty).where(
            FinanceBillingParty.organization_id == organization_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_id(
        self,
        billing_party_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FinanceBillingParty | None:
        statement = select(FinanceBillingParty).where(
            FinanceBillingParty.id == billing_party_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def create_billing_party(
        self,
        *,
        organization_id: uuid.UUID,
        billing_name: str,
        party_type: str,
        gst_treatment: str,
        billing_address: str,
        place_of_supply_state_code: str,
        status: str,
        gstin: str | None,
        pan: str | None,
        metadata_json: dict[str, object],
    ) -> FinanceBillingParty:
        party = FinanceBillingParty(
            organization_id=organization_id,
            buyer_kind="organization",
            billing_name=billing_name,
            party_type=party_type,
            gst_treatment=gst_treatment,
            gstin=gstin,
            pan=pan,
            billing_address=billing_address,
            place_of_supply_state_code=place_of_supply_state_code,
            status=status,
            metadata_json=metadata_json,
        )
        self._session.add(party)
        await self._session.flush()
        return party
