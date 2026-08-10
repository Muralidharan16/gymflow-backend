from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.billing_parties import FinanceBillingPartyError
from app.finance_core.models.foundation import FinanceBillingParty, FinanceIdempotencyKey
from app.models.organization import Organization


class FinanceBillingPartyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_organization(
        self,
        organization_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Organization | None:
        """Read the tenant root, optionally serialized by an advisory tx lock.

        Finance runtime intentionally has SELECT-only access to ``organizations``.
        A PostgreSQL ``SELECT ... FOR UPDATE`` would require UPDATE privilege on
        that tenant-root table and would broaden Finance capability beyond its
        business need. When serialization is requested, take the same bounded
        transaction-scoped advisory lock used for organization-bound billing-party
        creation, then perform an ordinary SELECT.
        """
        if for_update:
            await self.acquire_organization_creation_lock(organization_id)
        statement = select(Organization).where(Organization.id == organization_id)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

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
        statement = (
            insert(FinanceIdempotencyKey)
            .values(
                organization_id=organization_id,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash_sha256=request_hash,
                status="processing",
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_finance_idempotency_keys_scope_key"
            )
            .returning(FinanceIdempotencyKey)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True

        existing_result = await self._session.execute(
            select(FinanceIdempotencyKey)
            .where(
                FinanceIdempotencyKey.scope == scope,
                FinanceIdempotencyKey.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        existing = existing_result.scalar_one()
        if existing.request_hash_sha256 != request_hash:
            raise FinanceBillingPartyError(
                "BILLING_PARTY_IDEMPOTENCY_CONFLICT",
                "Billing party request idempotency key has already been used for a different request.",
            )
        return existing, False

    async def complete_idempotency_key(
        self,
        key: FinanceIdempotencyKey,
        *,
        response_ref: str,
    ) -> None:
        key.status = "succeeded"
        key.response_ref = response_ref
        await self._session.flush()

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
