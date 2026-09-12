from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

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
        try:
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
        except IntegrityError as exc:
            sqlstate = (
                getattr(exc.orig, "sqlstate", None)
                or getattr(exc.orig, "pgcode", None)
            )
            if (
                sqlstate != "23505"
                or "P4D finance idempotency request conflict" not in str(exc)
            ):
                raise
            raise FinanceCheckoutIntentConflictError(
                "Checkout intent conflicts with the existing idempotency key"
            ) from exc
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

    async def complete_idempotency_key(self, key: FinanceIdempotencyKey, *, response_ref: str) -> None:
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
