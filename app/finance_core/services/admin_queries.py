from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.admin_read_models import (
    AccountBalanceSummary,
    CorrectionHistoryItem,
    InvoiceSummary,
    LedgerEntrySummary,
    OutboxTraceItem,
    PageRequest,
    PaymentSummary,
    SettlementHistoryItem,
)
from app.finance_core.repositories.admin_queries import FinanceAdminQueryRepository


class FinanceAdminQueryService:
    def __init__(self, session: AsyncSession):
        self._repo = FinanceAdminQueryRepository(session)

    async def invoice_summary(self, invoice_id: uuid.UUID) -> InvoiceSummary | None:
        return await self._repo.invoice_summary(invoice_id)

    async def list_invoices(
        self,
        *,
        status: str | None = None,
        payment_status: str | None = None,
        page: PageRequest | None = None,
    ) -> list[InvoiceSummary]:
        return await self._repo.list_invoices(status=status, payment_status=payment_status, page=page)

    async def payment_summary(self, payment_id: uuid.UUID) -> PaymentSummary | None:
        return await self._repo.payment_summary(payment_id)

    async def list_payments(
        self,
        *,
        status: str | None = None,
        provider_code: str | None = None,
        page: PageRequest | None = None,
    ) -> list[PaymentSummary]:
        return await self._repo.list_payments(status=status, provider_code=provider_code, page=page)

    async def settlement_history_for_payment(self, payment_id: uuid.UUID) -> list[SettlementHistoryItem]:
        return await self._repo.settlement_history_for_payment(payment_id)

    async def correction_history(
        self,
        *,
        invoice_id: uuid.UUID | None = None,
        payment_id: uuid.UUID | None = None,
    ) -> list[CorrectionHistoryItem]:
        return await self._repo.correction_history(invoice_id=invoice_id, payment_id=payment_id)

    async def ledger_entries_by_source(self, *, source_type: str, source_id: uuid.UUID) -> list[LedgerEntrySummary]:
        return await self._repo.ledger_entries_by_source(source_type=source_type, source_id=source_id)

    async def account_balances(self) -> list[AccountBalanceSummary]:
        return await self._repo.account_balances()

    async def outbox_trace(self, *, aggregate_type: str, aggregate_id: uuid.UUID) -> list[OutboxTraceItem]:
        return await self._repo.outbox_trace(aggregate_type=aggregate_type, aggregate_id=aggregate_id)
