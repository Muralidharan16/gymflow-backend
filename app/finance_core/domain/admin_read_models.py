from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


MAX_QUERY_LIMIT = 100
DEFAULT_QUERY_LIMIT = 50


@dataclass(frozen=True)
class PageRequest:
    limit: int = DEFAULT_QUERY_LIMIT
    offset: int = 0


@dataclass(frozen=True)
class InvoiceSummary:
    invoice_id: uuid.UUID
    status: str
    official_invoice_number: str | None
    brand_reference: str | None
    buyer_billing_name: str
    grand_total_amount: Decimal
    allocated_amount: Decimal
    credited_amount: Decimal
    currency_code: str
    issued_at: datetime | None


@dataclass(frozen=True)
class PaymentSummary:
    payment_id: uuid.UUID
    provider_code: str
    provider_payment_ref: str | None
    provider_order_ref: str | None
    status: str
    amount: Decimal
    allocated_amount: Decimal
    refunded_amount: Decimal
    currency_code: str
    created_at: datetime


@dataclass(frozen=True)
class SettlementHistoryItem:
    event_id: uuid.UUID
    payment_id: uuid.UUID
    settlement_ref: str
    settlement_amount: Decimal
    gateway_fee_amount: Decimal
    ledger_entry_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class CorrectionHistoryItem:
    item_id: uuid.UUID
    item_type: str
    ref: str | None
    amount: Decimal
    status: str
    created_at: datetime


@dataclass(frozen=True)
class LedgerEntrySummary:
    ledger_entry_id: uuid.UUID
    entry_type: str
    source_type: str
    source_id: uuid.UUID
    status: str
    debit_total: Decimal
    credit_total: Decimal
    posted_at: datetime | None


@dataclass(frozen=True)
class AccountBalanceSummary:
    account_code: str
    account_name: str
    debit_total: Decimal
    credit_total: Decimal
    net_balance: Decimal


@dataclass(frozen=True)
class OutboxTraceItem:
    event_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    status: str
    payload: dict[str, Any]
    created_at: datetime


def normalized_page(page: PageRequest | None = None) -> PageRequest:
    page = page or PageRequest()
    limit = max(1, min(page.limit, MAX_QUERY_LIMIT))
    offset = max(0, page.offset)
    return PageRequest(limit=limit, offset=offset)
