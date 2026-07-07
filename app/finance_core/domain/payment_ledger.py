from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.finance_core.domain.invoice_engine import FinanceInvoiceValidationError, money


CAPTURED_PAYMENT_STATUSES = {"captured", "settled"}
FINAL_UNALLOCATABLE_PAYMENT_STATUSES = {"pending", "failed", "cancelled", "refunded", "partially_refunded"}


class FinancePaymentConflictError(Exception):
    pass


class FinancePaymentNotFoundError(Exception):
    pass


class FinancePaymentStateError(Exception):
    pass


class FinanceLedgerValidationError(Exception):
    pass


@dataclass(frozen=True)
class RecordPaymentCommand:
    organization_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID | None
    division_id: uuid.UUID | None
    brand_id: uuid.UUID | None
    provider_code: str
    provider_payment_ref: str | None
    provider_order_ref: str | None
    provider_signature_hash: str | None
    amount: Decimal
    currency_code: str
    status: str
    raw_status: str | None
    idempotency_key: str


@dataclass(frozen=True)
class RecordPaymentEventCommand:
    payment_id: uuid.UUID | None
    provider_code: str
    provider_event_id: str
    event_type: str
    event_payload_sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class AllocatePaymentCommand:
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    idempotency_key: str


@dataclass(frozen=True)
class ApplyPaymentToInvoiceCommand:
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    idempotency_key: str


@dataclass(frozen=True)
class ReconcilePaymentSettlementCommand:
    payment_id: uuid.UUID
    settlement_ref: str
    settlement_amount: Decimal
    gateway_fee_amount: Decimal = Decimal("0.00")
    idempotency_key: str = ""


@dataclass(frozen=True)
class CreateCreditNoteCommand:
    invoice_id: uuid.UUID
    credit_note_ref: str
    amount: Decimal
    reason: str
    idempotency_key: str


@dataclass(frozen=True)
class CreateRefundIntentCommand:
    payment_id: uuid.UUID
    refund_ref: str
    amount: Decimal
    reason: str
    credit_note_id: uuid.UUID | None
    idempotency_key: str


@dataclass(frozen=True)
class LedgerLineInput:
    account_code: str
    debit_amount: Decimal = Decimal("0.00")
    credit_amount: Decimal = Decimal("0.00")
    memo: str | None = None


@dataclass(frozen=True)
class PostLedgerEntryCommand:
    legal_entity_id: uuid.UUID
    division_id: uuid.UUID | None
    brand_id: uuid.UUID | None
    entry_type: str
    source_type: str
    source_id: uuid.UUID
    idempotency_key: str
    lines: tuple[LedgerLineInput, ...]


@dataclass(frozen=True)
class PaymentResult:
    payment_id: uuid.UUID
    status: str
    amount: Decimal
    allocated_amount: Decimal
    replayed: bool = False


@dataclass(frozen=True)
class PaymentEventResult:
    payment_event_id: uuid.UUID
    replayed: bool = False


@dataclass(frozen=True)
class PaymentAllocationResult:
    allocation_id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_status: str
    allocated_amount: Decimal
    replayed: bool = False


@dataclass(frozen=True)
class LedgerEntryResult:
    ledger_entry_id: uuid.UUID
    status: str
    replayed: bool = False


@dataclass(frozen=True)
class PaymentSettlementResult:
    payment_id: uuid.UUID
    settlement_ref: str
    ledger_entry_id: uuid.UUID
    settlement_amount: Decimal
    gateway_fee_amount: Decimal
    replayed: bool = False


@dataclass(frozen=True)
class CreditNoteResult:
    credit_note_id: uuid.UUID
    invoice_id: uuid.UUID
    credit_note_ref: str
    amount: Decimal
    ledger_entry_id: uuid.UUID
    replayed: bool = False


@dataclass(frozen=True)
class RefundIntentResult:
    refund_id: uuid.UUID
    payment_id: uuid.UUID
    refund_ref: str
    amount: Decimal
    status: str
    replayed: bool = False


def validate_money_amount(value: Decimal, label: str) -> Decimal:
    amount = money(value)
    if amount <= 0:
        raise FinanceInvoiceValidationError(f"{label} must be positive")
    return amount


def validate_ledger_lines(lines: tuple[LedgerLineInput, ...]) -> tuple[LedgerLineInput, ...]:
    if len(lines) < 2:
        raise FinanceLedgerValidationError("Ledger entry requires at least two lines")

    debit_total = Decimal("0.00")
    credit_total = Decimal("0.00")
    normalized: list[LedgerLineInput] = []
    for line in lines:
        debit = money(line.debit_amount)
        credit = money(line.credit_amount)
        if debit < 0 or credit < 0:
            raise FinanceLedgerValidationError("Ledger line amounts cannot be negative")
        if (debit == 0 and credit == 0) or (debit > 0 and credit > 0):
            raise FinanceLedgerValidationError("Ledger line must be one-sided")
        debit_total += debit
        credit_total += credit
        normalized.append(
            LedgerLineInput(
                account_code=line.account_code,
                debit_amount=debit,
                credit_amount=credit,
                memo=line.memo,
            )
        )

    if money(debit_total) != money(credit_total):
        raise FinanceLedgerValidationError("Ledger entry must balance")
    return tuple(normalized)
