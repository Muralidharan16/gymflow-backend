from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text
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
    normalized_page,
)


class FinanceAdminQueryRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def invoice_summary(self, invoice_id: uuid.UUID) -> InvoiceSummary | None:
        result = await self._session.execute(
            text(
                """
                SELECT
                    invoice.id AS invoice_id,
                    invoice.status,
                    invoice.official_invoice_number,
                    invoice.brand_reference,
                    invoice.buyer_billing_name,
                    invoice.grand_total_amount,
                    invoice.currency_code,
                    invoice.issued_at,
                    coalesce(allocations.allocated_amount, 0) AS allocated_amount,
                    coalesce(credits.credited_amount, 0) AS credited_amount
                FROM finance.invoices invoice
                LEFT JOIN (
                    SELECT invoice_id, sum(allocated_amount) AS allocated_amount
                    FROM finance.payment_allocations
                    GROUP BY invoice_id
                ) allocations ON allocations.invoice_id = invoice.id
                LEFT JOIN (
                    SELECT invoice_id, sum(total_amount) AS credited_amount
                    FROM finance.credit_notes
                    WHERE status <> 'voided'
                    GROUP BY invoice_id
                ) credits ON credits.invoice_id = invoice.id
                WHERE invoice.id = :invoice_id
                """
            ),
            {"invoice_id": invoice_id},
        )
        row = result.mappings().one_or_none()
        return _invoice_summary(row) if row else None

    async def list_invoices(
        self,
        *,
        status: str | None = None,
        payment_status: str | None = None,
        page: PageRequest | None = None,
    ) -> list[InvoiceSummary]:
        page = normalized_page(page)
        result = await self._session.execute(
            text(
                """
                SELECT
                    invoice.id AS invoice_id,
                    invoice.status,
                    invoice.official_invoice_number,
                    invoice.brand_reference,
                    invoice.buyer_billing_name,
                    invoice.grand_total_amount,
                    invoice.currency_code,
                    invoice.issued_at,
                    coalesce(allocations.allocated_amount, 0) AS allocated_amount,
                    coalesce(credits.credited_amount, 0) AS credited_amount
                FROM finance.invoices invoice
                LEFT JOIN (
                    SELECT invoice_id, sum(allocated_amount) AS allocated_amount
                    FROM finance.payment_allocations
                    GROUP BY invoice_id
                ) allocations ON allocations.invoice_id = invoice.id
                LEFT JOIN (
                    SELECT invoice_id, sum(total_amount) AS credited_amount
                    FROM finance.credit_notes
                    WHERE status <> 'voided'
                    GROUP BY invoice_id
                ) credits ON credits.invoice_id = invoice.id
                WHERE (:use_status_filter = false OR invoice.status = :status)
                  AND (
                    :use_payment_status_filter = false
                    OR EXISTS (
                        SELECT 1
                        FROM finance.payment_allocations allocation
                        JOIN finance.payments payment ON payment.id = allocation.payment_id
                        WHERE allocation.invoice_id = invoice.id
                          AND payment.status = :payment_status
                    )
                  )
                ORDER BY invoice.created_at DESC, invoice.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "status": status,
                "payment_status": payment_status,
                "use_status_filter": status is not None,
                "use_payment_status_filter": payment_status is not None,
                "limit": page.limit,
                "offset": page.offset,
            },
        )
        return [_invoice_summary(row) for row in result.mappings()]

    async def payment_summary(self, payment_id: uuid.UUID) -> PaymentSummary | None:
        result = await self._session.execute(
            text(
                """
                SELECT
                    payment.id AS payment_id,
                    payment.provider_code,
                    payment.provider_payment_ref,
                    payment.provider_order_ref,
                    payment.status,
                    payment.amount,
                    payment.currency_code,
                    payment.created_at,
                    coalesce(allocations.allocated_amount, 0) AS allocated_amount,
                    coalesce(refunds.refunded_amount, 0) AS refunded_amount
                FROM finance.payments payment
                LEFT JOIN (
                    SELECT payment_id, sum(allocated_amount) AS allocated_amount
                    FROM finance.payment_allocations
                    GROUP BY payment_id
                ) allocations ON allocations.payment_id = payment.id
                LEFT JOIN (
                    SELECT payment_id, sum(amount) AS refunded_amount
                    FROM finance.refunds
                    WHERE status <> 'cancelled'
                    GROUP BY payment_id
                ) refunds ON refunds.payment_id = payment.id
                WHERE payment.id = :payment_id
                """
            ),
            {"payment_id": payment_id},
        )
        row = result.mappings().one_or_none()
        return _payment_summary(row) if row else None

    async def list_payments(
        self,
        *,
        status: str | None = None,
        provider_code: str | None = None,
        page: PageRequest | None = None,
    ) -> list[PaymentSummary]:
        page = normalized_page(page)
        result = await self._session.execute(
            text(
                """
                SELECT
                    payment.id AS payment_id,
                    payment.provider_code,
                    payment.provider_payment_ref,
                    payment.provider_order_ref,
                    payment.status,
                    payment.amount,
                    payment.currency_code,
                    payment.created_at,
                    coalesce(allocations.allocated_amount, 0) AS allocated_amount,
                    coalesce(refunds.refunded_amount, 0) AS refunded_amount
                FROM finance.payments payment
                LEFT JOIN (
                    SELECT payment_id, sum(allocated_amount) AS allocated_amount
                    FROM finance.payment_allocations
                    GROUP BY payment_id
                ) allocations ON allocations.payment_id = payment.id
                LEFT JOIN (
                    SELECT payment_id, sum(amount) AS refunded_amount
                    FROM finance.refunds
                    WHERE status <> 'cancelled'
                    GROUP BY payment_id
                ) refunds ON refunds.payment_id = payment.id
                WHERE (:use_status_filter = false OR payment.status = :status)
                  AND (:use_provider_filter = false OR payment.provider_code = :provider_code)
                ORDER BY payment.created_at DESC, payment.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "status": status,
                "provider_code": provider_code,
                "use_status_filter": status is not None,
                "use_provider_filter": provider_code is not None,
                "limit": page.limit,
                "offset": page.offset,
            },
        )
        return [_payment_summary(row) for row in result.mappings()]

    async def settlement_history_for_payment(self, payment_id: uuid.UUID) -> list[SettlementHistoryItem]:
        result = await self._session.execute(
            text(
                """
                SELECT
                    id AS event_id,
                    aggregate_id AS payment_id,
                    payload_json->>'settlement_ref' AS settlement_ref,
                    (payload_json->>'settlement_amount')::numeric(14, 2) AS settlement_amount,
                    (payload_json->>'gateway_fee_amount')::numeric(14, 2) AS gateway_fee_amount,
                    (payload_json->>'ledger_entry_id')::uuid AS ledger_entry_id,
                    created_at
                FROM finance.outbox_events
                WHERE aggregate_type = 'payment'
                  AND aggregate_id = :payment_id
                  AND event_type = 'finance.payment.reconciled'
                ORDER BY created_at DESC, id DESC
                """
            ),
            {"payment_id": payment_id},
        )
        return [
            SettlementHistoryItem(
                event_id=row["event_id"],
                payment_id=row["payment_id"],
                settlement_ref=row["settlement_ref"],
                settlement_amount=Decimal(row["settlement_amount"]),
                gateway_fee_amount=Decimal(row["gateway_fee_amount"]),
                ledger_entry_id=row["ledger_entry_id"],
                created_at=row["created_at"],
            )
            for row in result.mappings()
        ]

    async def correction_history(
        self,
        *,
        invoice_id: uuid.UUID | None = None,
        payment_id: uuid.UUID | None = None,
    ) -> list[CorrectionHistoryItem]:
        result = await self._session.execute(
            text(
                """
                SELECT id AS item_id, 'credit_note' AS item_type, credit_note_number AS ref,
                       total_amount AS amount, status, created_at
                FROM finance.credit_notes
                WHERE (:use_invoice_filter = false OR invoice_id = :invoice_id)
                  AND :use_payment_filter = false
                UNION ALL
                SELECT id AS item_id, 'refund' AS item_type, reason_code AS ref,
                       amount, status, created_at
                FROM finance.refunds
                WHERE (:use_payment_filter = false OR payment_id = :payment_id)
                  AND :use_invoice_filter = false
                ORDER BY created_at DESC, item_id DESC
                """
            ),
            {
                "invoice_id": invoice_id,
                "payment_id": payment_id,
                "use_invoice_filter": invoice_id is not None,
                "use_payment_filter": payment_id is not None,
            },
        )
        return [
            CorrectionHistoryItem(
                item_id=row["item_id"],
                item_type=row["item_type"],
                ref=row["ref"],
                amount=Decimal(row["amount"]),
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in result.mappings()
        ]

    async def ledger_entries_by_source(self, *, source_type: str, source_id: uuid.UUID) -> list[LedgerEntrySummary]:
        result = await self._session.execute(
            text(
                """
                SELECT
                    entry.id AS ledger_entry_id,
                    entry.entry_type,
                    entry.source_type,
                    entry.source_id,
                    entry.status,
                    entry.posted_at,
                    coalesce(sum(line.debit_amount), 0) AS debit_total,
                    coalesce(sum(line.credit_amount), 0) AS credit_total
                FROM finance.ledger_entries entry
                JOIN finance.ledger_entry_lines line ON line.ledger_entry_id = entry.id
                WHERE entry.source_type = :source_type
                  AND entry.source_id = :source_id
                GROUP BY entry.id
                ORDER BY entry.created_at DESC, entry.id DESC
                """
            ),
            {"source_type": source_type, "source_id": source_id},
        )
        return [_ledger_entry_summary(row) for row in result.mappings()]

    async def account_balances(self) -> list[AccountBalanceSummary]:
        result = await self._session.execute(
            text(
                """
                SELECT
                    account.code AS account_code,
                    account.name AS account_name,
                    coalesce(sum(line.debit_amount), 0) AS debit_total,
                    coalesce(sum(line.credit_amount), 0) AS credit_total
                FROM finance.ledger_accounts account
                LEFT JOIN finance.ledger_entry_lines line ON line.ledger_account_id = account.id
                GROUP BY account.code, account.name
                ORDER BY account.code ASC
                """
            )
        )
        return [
            AccountBalanceSummary(
                account_code=row["account_code"],
                account_name=row["account_name"],
                debit_total=Decimal(row["debit_total"]),
                credit_total=Decimal(row["credit_total"]),
                net_balance=Decimal(row["debit_total"]) - Decimal(row["credit_total"]),
            )
            for row in result.mappings()
        ]

    async def outbox_trace(self, *, aggregate_type: str, aggregate_id: uuid.UUID) -> list[OutboxTraceItem]:
        result = await self._session.execute(
            text(
                """
                SELECT id AS event_id, aggregate_type, aggregate_id, event_type,
                       status, payload_json, created_at
                FROM finance.outbox_events
                WHERE aggregate_type = :aggregate_type
                  AND aggregate_id = :aggregate_id
                ORDER BY created_at DESC, id DESC
                """
            ),
            {"aggregate_type": aggregate_type, "aggregate_id": aggregate_id},
        )
        return [
            OutboxTraceItem(
                event_id=row["event_id"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                event_type=row["event_type"],
                status=row["status"],
                payload=dict(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in result.mappings()
        ]


def _invoice_summary(row) -> InvoiceSummary:
    return InvoiceSummary(
        invoice_id=row["invoice_id"],
        status=row["status"],
        official_invoice_number=row["official_invoice_number"],
        brand_reference=row["brand_reference"],
        buyer_billing_name=row["buyer_billing_name"],
        grand_total_amount=Decimal(row["grand_total_amount"]),
        allocated_amount=Decimal(row["allocated_amount"]),
        credited_amount=Decimal(row["credited_amount"]),
        currency_code=row["currency_code"],
        issued_at=row["issued_at"],
    )


def _payment_summary(row) -> PaymentSummary:
    return PaymentSummary(
        payment_id=row["payment_id"],
        provider_code=row["provider_code"],
        provider_payment_ref=row["provider_payment_ref"],
        provider_order_ref=row["provider_order_ref"],
        status=row["status"],
        amount=Decimal(row["amount"]),
        allocated_amount=Decimal(row["allocated_amount"]),
        refunded_amount=Decimal(row["refunded_amount"]),
        currency_code=row["currency_code"],
        created_at=row["created_at"],
    )


def _ledger_entry_summary(row) -> LedgerEntrySummary:
    return LedgerEntrySummary(
        ledger_entry_id=row["ledger_entry_id"],
        entry_type=row["entry_type"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        status=row["status"],
        debit_total=Decimal(row["debit_total"]),
        credit_total=Decimal(row["credit_total"]),
        posted_at=row["posted_at"],
    )
