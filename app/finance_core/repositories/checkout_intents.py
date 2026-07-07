from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.provider_boundary import FinanceCheckoutIntentConflictError
from app.finance_core.models.foundation import (
    FinanceIdempotencyKey,
    FinanceInvoice,
    FinanceOutboxEvent,
    FinancePayment,
)


class FinanceCheckoutIntentRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def reserve_idempotency_key(
        self,
        *,
        organization_id: uuid.UUID | None,
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
            .on_conflict_do_nothing(constraint="uq_finance_idempotency_keys_scope_key")
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
            raise FinanceCheckoutIntentConflictError("Idempotency key already exists for a different checkout intent")
        return existing, False

    async def complete_idempotency_key(self, key: FinanceIdempotencyKey, *, response_ref: str) -> None:
        key.status = "succeeded"
        key.response_ref = response_ref
        await self._session.flush()

    async def get_invoice(self, invoice_id: uuid.UUID, *, for_update: bool = False) -> FinanceInvoice | None:
        statement = select(FinanceInvoice).where(FinanceInvoice.id == invoice_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_intent(self, intent_id: uuid.UUID, *, for_update: bool = False) -> FinancePayment | None:
        statement = select(FinancePayment).where(FinancePayment.id == intent_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def create_intent(
        self,
        *,
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID | None,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        idempotency_key_id: uuid.UUID,
        provider_code: str,
        provider_order_ref: str,
        amount: Decimal,
        currency_code: str,
    ) -> FinancePayment:
        intent = FinancePayment(
            organization_id=organization_id,
            legal_entity_id=legal_entity_id,
            gst_registration_id=gst_registration_id,
            division_id=division_id,
            brand_id=brand_id,
            idempotency_key_id=idempotency_key_id,
            provider_code=provider_code,
            provider_payment_ref=None,
            provider_order_ref=provider_order_ref,
            provider_signature_hash=None,
            amount=amount,
            currency_code=currency_code,
            status="created",
            raw_status="checkout_intent_created",
        )
        self._session.add(intent)
        await self._session.flush()
        return intent

    async def create_outbox_event(
        self,
        *,
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID | None,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        aggregate_id: uuid.UUID,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        self._session.add(
            FinanceOutboxEvent(
                organization_id=organization_id,
                legal_entity_id=legal_entity_id,
                division_id=division_id,
                brand_id=brand_id,
                aggregate_type="checkout_intent",
                aggregate_id=aggregate_id,
                event_type="finance.checkout_intent.created",
                idempotency_key=idempotency_key,
                payload_json=payload,
                payload_sha256=canonical_hash(payload),
                status="pending",
            )
        )
        await self._session.flush()
