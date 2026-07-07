from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Numeric, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import canonical_hash
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    LedgerLineInput,
)
from app.finance_core.models.foundation import (
    FinanceIdempotencyKey,
    FinanceInvoice,
    FinanceInvoiceLine,
    FinanceLedgerAccount,
    FinanceLedgerEntry,
    FinanceLedgerEntryLine,
    FinanceOutboxEvent,
    FinancePayment,
    FinancePaymentAllocation,
    FinancePaymentEvent,
)


class FinancePaymentRepository:
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
            raise FinancePaymentConflictError("Idempotency key already exists for a different finance request")
        return existing, False

    async def complete_idempotency_key(self, key: FinanceIdempotencyKey, *, response_ref: str) -> None:
        key.status = "succeeded"
        key.response_ref = response_ref
        await self._session.flush()

    async def get_payment(self, payment_id: uuid.UUID, *, for_update: bool = False) -> FinancePayment | None:
        statement = select(FinancePayment).where(FinancePayment.id == payment_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def update_payment_status(self, payment: FinancePayment, *, status: str, raw_status: str | None) -> None:
        payment.status = status
        payment.raw_status = raw_status
        payment.updated_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def get_payment_by_provider_ref(
        self,
        *,
        provider_code: str,
        provider_payment_ref: str,
        for_update: bool = False,
    ) -> FinancePayment | None:
        statement = select(FinancePayment).where(
            FinancePayment.provider_code == provider_code,
            FinancePayment.provider_payment_ref == provider_payment_ref,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def create_payment(
        self,
        *,
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID | None,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        idempotency_key_id: uuid.UUID,
        provider_code: str,
        provider_payment_ref: str | None,
        provider_order_ref: str | None,
        provider_signature_hash: str | None,
        amount: Decimal,
        currency_code: str,
        status: str,
        raw_status: str | None,
    ) -> FinancePayment:
        payment = FinancePayment(
            organization_id=organization_id,
            legal_entity_id=legal_entity_id,
            gst_registration_id=gst_registration_id,
            division_id=division_id,
            brand_id=brand_id,
            idempotency_key_id=idempotency_key_id,
            provider_code=provider_code,
            provider_payment_ref=provider_payment_ref,
            provider_order_ref=provider_order_ref,
            provider_signature_hash=provider_signature_hash,
            amount=amount,
            currency_code=currency_code,
            status=status,
            raw_status=raw_status,
        )
        self._session.add(payment)
        await self._session.flush()
        return payment

    async def create_payment_event(
        self,
        *,
        payment_id: uuid.UUID | None,
        provider_code: str,
        provider_event_id: str,
        event_type: str,
        event_payload_sha256: str,
    ) -> FinancePaymentEvent:
        event = FinancePaymentEvent(
            payment_id=payment_id,
            provider_code=provider_code,
            provider_event_id=provider_event_id,
            event_type=event_type,
            event_payload_sha256=event_payload_sha256,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def get_payment_event_by_provider_id(
        self,
        *,
        provider_code: str,
        provider_event_id: str,
        for_update: bool = False,
    ) -> FinancePaymentEvent | None:
        statement = select(FinancePaymentEvent).where(
            FinancePaymentEvent.provider_code == provider_code,
            FinancePaymentEvent.provider_event_id == provider_event_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_invoice(self, invoice_id: uuid.UUID, *, for_update: bool = False) -> FinanceInvoice | None:
        statement = select(FinanceInvoice).where(FinanceInvoice.id == invoice_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def allocated_payment_total(self, payment_id: uuid.UUID) -> Decimal:
        result = await self._session.execute(
            select(func.coalesce(func.sum(FinancePaymentAllocation.allocated_amount), 0)).where(
                FinancePaymentAllocation.payment_id == payment_id
            )
        )
        return Decimal(result.scalar_one())

    async def allocated_invoice_total(self, invoice_id: uuid.UUID) -> Decimal:
        result = await self._session.execute(
            select(func.coalesce(func.sum(FinancePaymentAllocation.allocated_amount), 0)).where(
                FinancePaymentAllocation.invoice_id == invoice_id
            )
        )
        return Decimal(result.scalar_one())

    async def reconciled_payment_total(self, payment_id: uuid.UUID) -> Decimal:
        result = await self._session.execute(
            select(
                func.coalesce(
                    func.sum(FinanceOutboxEvent.payload_json["settlement_amount"].astext.cast(Numeric(14, 2))),
                    0,
                )
            ).where(
                FinanceOutboxEvent.aggregate_type == "payment",
                FinanceOutboxEvent.aggregate_id == payment_id,
                FinanceOutboxEvent.event_type == "finance.payment.reconciled",
            )
        )
        return Decimal(result.scalar_one())

    async def invoice_tax_component_totals(self, invoice_id: uuid.UUID) -> dict[str, Decimal]:
        result = await self._session.execute(
            select(
                func.coalesce(func.sum(FinanceInvoiceLine.cgst_amount), 0),
                func.coalesce(func.sum(FinanceInvoiceLine.sgst_amount), 0),
                func.coalesce(func.sum(FinanceInvoiceLine.igst_amount), 0),
            ).where(FinanceInvoiceLine.invoice_id == invoice_id)
        )
        cgst, sgst, igst = result.one()
        return {
            "cgst": Decimal(cgst),
            "sgst": Decimal(sgst),
            "igst": Decimal(igst),
        }

    async def get_allocation(
        self,
        allocation_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FinancePaymentAllocation | None:
        statement = select(FinancePaymentAllocation).where(FinancePaymentAllocation.id == allocation_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_allocation_for_payment_invoice(
        self,
        *,
        payment_id: uuid.UUID,
        invoice_id: uuid.UUID,
        for_update: bool = False,
    ) -> FinancePaymentAllocation | None:
        statement = select(FinancePaymentAllocation).where(
            FinancePaymentAllocation.payment_id == payment_id,
            FinancePaymentAllocation.invoice_id == invoice_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def create_allocation(
        self,
        *,
        payment_id: uuid.UUID,
        invoice_id: uuid.UUID,
        amount: Decimal,
    ) -> FinancePaymentAllocation:
        allocation = FinancePaymentAllocation(
            payment_id=payment_id,
            invoice_id=invoice_id,
            allocated_amount=amount,
        )
        self._session.add(allocation)
        await self._session.flush()
        return allocation

    async def get_ledger_entry_by_source(
        self,
        *,
        source_type: str,
        source_id: uuid.UUID,
        for_update: bool = False,
    ) -> FinanceLedgerEntry | None:
        statement = select(FinanceLedgerEntry).where(
            FinanceLedgerEntry.source_type == source_type,
            FinanceLedgerEntry.source_id == source_id,
            FinanceLedgerEntry.status == "posted",
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_ledger_account(self, *, legal_entity_id: uuid.UUID, code: str) -> FinanceLedgerAccount:
        result = await self._session.execute(
            select(FinanceLedgerAccount).where(
                FinanceLedgerAccount.legal_entity_id == legal_entity_id,
                FinanceLedgerAccount.code == code,
            )
        )
        return result.scalar_one()

    async def create_ledger_entry(
        self,
        *,
        legal_entity_id: uuid.UUID,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        entry_type: str,
        source_type: str,
        source_id: uuid.UUID,
        lines: tuple[LedgerLineInput, ...],
    ) -> FinanceLedgerEntry:
        entry = FinanceLedgerEntry(
            legal_entity_id=legal_entity_id,
            division_id=division_id,
            brand_id=brand_id,
            entry_type=entry_type,
            source_type=source_type,
            source_id=source_id,
            status="posted",
            posted_at=datetime.now(timezone.utc),
        )
        self._session.add(entry)
        await self._session.flush()

        for line in lines:
            account = await self.get_ledger_account(legal_entity_id=legal_entity_id, code=line.account_code)
            self._session.add(
                FinanceLedgerEntryLine(
                    ledger_entry_id=entry.id,
                    ledger_account_id=account.id,
                    debit_amount=line.debit_amount,
                    credit_amount=line.credit_amount,
                    memo=line.memo,
                )
            )
        await self._session.flush()
        return entry

    async def create_outbox_event(
        self,
        *,
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID | None,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        self._session.add(
            FinanceOutboxEvent(
                organization_id=organization_id,
                legal_entity_id=legal_entity_id,
                division_id=division_id,
                brand_id=brand_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                idempotency_key=idempotency_key,
                payload_json=payload,
                payload_sha256=canonical_hash(payload),
                status="pending",
            )
        )
        await self._session.flush()
