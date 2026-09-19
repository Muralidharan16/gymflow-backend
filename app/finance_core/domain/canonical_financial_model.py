from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping, TypeVar


class CanonicalFinancialModelError(ValueError):
    """Raised when PAY-1 canonical financial-domain invariants are violated."""


class InvalidFinancialTransition(CanonicalFinancialModelError):
    pass


class MoneyValidationError(CanonicalFinancialModelError):
    pass


class TransitionAction(str, Enum):
    APPLY = "apply"
    NOOP = "noop"


class PaymentStatus(str, Enum):
    CREATED = "created"
    PENDING = "pending"
    REQUIRES_ACTION = "requires_action"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentAttemptStatus(str, Enum):
    CREATED = "created"
    SUBMITTING = "submitting"
    PROVIDER_PENDING = "provider_pending"
    REQUIRES_ACTION = "requires_action"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    UNKNOWN = "unknown"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentEvidenceVerdict(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CANCELLED = "cancelled"
    VOIDED = "voided"
    CREDITED = "credited"


class InvoiceDueState(str, Enum):
    NOT_DUE = "not_due"
    DUE = "due"
    OVERDUE = "overdue"
    SATISFIED = "satisfied"


class AllocationStatus(str, Enum):
    APPLIED = "applied"
    PARTIALLY_REVERSED = "partially_reversed"
    REVERSED = "reversed"


class SettlementStatus(str, Enum):
    REPORTED = "reported"
    VERIFIED = "verified"
    PARTIALLY_RECONCILED = "partially_reconciled"
    RECONCILED = "reconciled"
    DISCREPANT = "discrepant"


class SettlementKind(str, Enum):
    CREDIT = "credit"
    DEBIT_ADJUSTMENT = "debit_adjustment"
    REVERSAL = "reversal"


class CreditNoteStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    VOIDED = "voided"


class RefundStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RefundExecutionStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_PENDING = "retry_pending"
    PROVIDER_ACCEPTED = "provider_accepted"
    RECONCILIATION_PENDING = "reconciliation_pending"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class DisputeStatus(str, Enum):
    OPENED = "opened"
    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_SUBMITTED = "evidence_submitted"
    UNDER_REVIEW = "under_review"
    WON = "won"
    LOST = "lost"
    CLOSED = "closed"


class ChargebackStatus(str, Enum):
    PENDING = "pending"
    DEBITED = "debited"
    REVERSED = "reversed"
    FINAL_LOSS = "final_loss"


class AdjustmentStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    POSTED = "posted"


class ProviderOperationStatus(str, Enum):
    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


PAYMENT_TRANSITIONS: Mapping[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({
        PaymentStatus.PENDING,
        PaymentStatus.REQUIRES_ACTION,
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    }),
    PaymentStatus.PENDING: frozenset({
        PaymentStatus.REQUIRES_ACTION,
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    }),
    PaymentStatus.REQUIRES_ACTION: frozenset({
        PaymentStatus.PENDING,
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    }),
    PaymentStatus.AUTHORIZED: frozenset({
        PaymentStatus.CAPTURED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    }),
    PaymentStatus.CAPTURED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
}

PAYMENT_ATTEMPT_TRANSITIONS: Mapping[PaymentAttemptStatus, frozenset[PaymentAttemptStatus]] = {
    PaymentAttemptStatus.CREATED: frozenset({
        PaymentAttemptStatus.SUBMITTING,
        PaymentAttemptStatus.CANCELLED,
        PaymentAttemptStatus.EXPIRED,
    }),
    PaymentAttemptStatus.SUBMITTING: frozenset({
        PaymentAttemptStatus.PROVIDER_PENDING,
        PaymentAttemptStatus.REQUIRES_ACTION,
        PaymentAttemptStatus.AUTHORIZED,
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.UNKNOWN,
        PaymentAttemptStatus.FAILED_RETRYABLE,
        PaymentAttemptStatus.FAILED_FINAL,
        PaymentAttemptStatus.CANCELLED,
    }),
    PaymentAttemptStatus.PROVIDER_PENDING: frozenset({
        PaymentAttemptStatus.REQUIRES_ACTION,
        PaymentAttemptStatus.AUTHORIZED,
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.UNKNOWN,
        PaymentAttemptStatus.FAILED_RETRYABLE,
        PaymentAttemptStatus.FAILED_FINAL,
        PaymentAttemptStatus.CANCELLED,
        PaymentAttemptStatus.EXPIRED,
    }),
    PaymentAttemptStatus.REQUIRES_ACTION: frozenset({
        PaymentAttemptStatus.PROVIDER_PENDING,
        PaymentAttemptStatus.AUTHORIZED,
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.UNKNOWN,
        PaymentAttemptStatus.FAILED_RETRYABLE,
        PaymentAttemptStatus.FAILED_FINAL,
        PaymentAttemptStatus.CANCELLED,
        PaymentAttemptStatus.EXPIRED,
    }),
    PaymentAttemptStatus.AUTHORIZED: frozenset({
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.UNKNOWN,
        PaymentAttemptStatus.CANCELLED,
        PaymentAttemptStatus.EXPIRED,
    }),
    PaymentAttemptStatus.UNKNOWN: frozenset({
        PaymentAttemptStatus.PROVIDER_PENDING,
        PaymentAttemptStatus.REQUIRES_ACTION,
        PaymentAttemptStatus.AUTHORIZED,
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.FAILED_RETRYABLE,
        PaymentAttemptStatus.FAILED_FINAL,
        PaymentAttemptStatus.CANCELLED,
        PaymentAttemptStatus.EXPIRED,
    }),
    PaymentAttemptStatus.CAPTURED: frozenset(),
    PaymentAttemptStatus.FAILED_RETRYABLE: frozenset(),
    PaymentAttemptStatus.FAILED_FINAL: frozenset(),
    PaymentAttemptStatus.CANCELLED: frozenset(),
    PaymentAttemptStatus.EXPIRED: frozenset(),
}

INVOICE_TRANSITIONS: Mapping[InvoiceStatus, frozenset[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.ISSUED, InvoiceStatus.VOIDED}),
    InvoiceStatus.ISSUED: frozenset({
        InvoiceStatus.PARTIALLY_PAID,
        InvoiceStatus.PAID,
        InvoiceStatus.CANCELLED,
        InvoiceStatus.CREDITED,
    }),
    InvoiceStatus.PARTIALLY_PAID: frozenset({
        InvoiceStatus.PAID,
        InvoiceStatus.CREDITED,
    }),
    InvoiceStatus.PAID: frozenset({InvoiceStatus.CREDITED}),
    InvoiceStatus.CANCELLED: frozenset(),
    InvoiceStatus.VOIDED: frozenset(),
    InvoiceStatus.CREDITED: frozenset(),
}

ALLOCATION_TRANSITIONS: Mapping[AllocationStatus, frozenset[AllocationStatus]] = {
    AllocationStatus.APPLIED: frozenset({
        AllocationStatus.PARTIALLY_REVERSED,
        AllocationStatus.REVERSED,
    }),
    AllocationStatus.PARTIALLY_REVERSED: frozenset({AllocationStatus.REVERSED}),
    AllocationStatus.REVERSED: frozenset(),
}

SETTLEMENT_TRANSITIONS: Mapping[SettlementStatus, frozenset[SettlementStatus]] = {
    SettlementStatus.REPORTED: frozenset({
        SettlementStatus.VERIFIED,
        SettlementStatus.DISCREPANT,
    }),
    SettlementStatus.VERIFIED: frozenset({
        SettlementStatus.PARTIALLY_RECONCILED,
        SettlementStatus.RECONCILED,
        SettlementStatus.DISCREPANT,
    }),
    SettlementStatus.PARTIALLY_RECONCILED: frozenset({
        SettlementStatus.RECONCILED,
        SettlementStatus.DISCREPANT,
    }),
    SettlementStatus.DISCREPANT: frozenset({
        SettlementStatus.VERIFIED,
        SettlementStatus.PARTIALLY_RECONCILED,
        SettlementStatus.RECONCILED,
    }),
    SettlementStatus.RECONCILED: frozenset(),
}

CREDIT_NOTE_TRANSITIONS: Mapping[CreditNoteStatus, frozenset[CreditNoteStatus]] = {
    CreditNoteStatus.DRAFT: frozenset({
        CreditNoteStatus.ISSUED,
        CreditNoteStatus.VOIDED,
    }),
    CreditNoteStatus.ISSUED: frozenset(),
    CreditNoteStatus.VOIDED: frozenset(),
}

REFUND_TRANSITIONS: Mapping[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.REQUESTED: frozenset({
        RefundStatus.APPROVED,
        RefundStatus.REJECTED,
        RefundStatus.CANCELLED,
    }),
    RefundStatus.APPROVED: frozenset({
        RefundStatus.PROCESSING,
        RefundStatus.CANCELLED,
    }),
    RefundStatus.PROCESSING: frozenset({
        RefundStatus.SUCCEEDED,
        RefundStatus.FAILED,
    }),
    RefundStatus.REJECTED: frozenset(),
    RefundStatus.SUCCEEDED: frozenset(),
    RefundStatus.FAILED: frozenset(),
    RefundStatus.CANCELLED: frozenset(),
}

REFUND_EXECUTION_TRANSITIONS: Mapping[RefundExecutionStatus, frozenset[RefundExecutionStatus]] = {
    RefundExecutionStatus.PENDING: frozenset({
        RefundExecutionStatus.PROCESSING,
        RefundExecutionStatus.CANCELLED,
    }),
    RefundExecutionStatus.PROCESSING: frozenset({
        RefundExecutionStatus.RETRY_PENDING,
        RefundExecutionStatus.PROVIDER_ACCEPTED,
        RefundExecutionStatus.RECONCILIATION_PENDING,
        RefundExecutionStatus.UNKNOWN,
        RefundExecutionStatus.SUCCEEDED,
        RefundExecutionStatus.REJECTED,
        RefundExecutionStatus.DEAD_LETTERED,
    }),
    RefundExecutionStatus.RETRY_PENDING: frozenset({
        RefundExecutionStatus.PROCESSING,
        RefundExecutionStatus.CANCELLED,
        RefundExecutionStatus.DEAD_LETTERED,
    }),
    RefundExecutionStatus.PROVIDER_ACCEPTED: frozenset({
        RefundExecutionStatus.RECONCILIATION_PENDING,
        RefundExecutionStatus.UNKNOWN,
        RefundExecutionStatus.SUCCEEDED,
        RefundExecutionStatus.REJECTED,
    }),
    RefundExecutionStatus.RECONCILIATION_PENDING: frozenset({
        RefundExecutionStatus.RETRY_PENDING,
        RefundExecutionStatus.UNKNOWN,
        RefundExecutionStatus.SUCCEEDED,
        RefundExecutionStatus.REJECTED,
        RefundExecutionStatus.DEAD_LETTERED,
    }),
    RefundExecutionStatus.UNKNOWN: frozenset({
        RefundExecutionStatus.RECONCILIATION_PENDING,
        RefundExecutionStatus.RETRY_PENDING,
        RefundExecutionStatus.SUCCEEDED,
        RefundExecutionStatus.REJECTED,
        RefundExecutionStatus.DEAD_LETTERED,
    }),
    RefundExecutionStatus.SUCCEEDED: frozenset(),
    RefundExecutionStatus.REJECTED: frozenset(),
    RefundExecutionStatus.DEAD_LETTERED: frozenset(),
    RefundExecutionStatus.CANCELLED: frozenset(),
}

DISPUTE_TRANSITIONS: Mapping[DisputeStatus, frozenset[DisputeStatus]] = {
    DisputeStatus.OPENED: frozenset({
        DisputeStatus.EVIDENCE_REQUIRED,
        DisputeStatus.EVIDENCE_SUBMITTED,
        DisputeStatus.UNDER_REVIEW,
        DisputeStatus.WON,
        DisputeStatus.LOST,
        DisputeStatus.CLOSED,
    }),
    DisputeStatus.EVIDENCE_REQUIRED: frozenset({
        DisputeStatus.EVIDENCE_SUBMITTED,
        DisputeStatus.UNDER_REVIEW,
        DisputeStatus.WON,
        DisputeStatus.LOST,
        DisputeStatus.CLOSED,
    }),
    DisputeStatus.EVIDENCE_SUBMITTED: frozenset({
        DisputeStatus.UNDER_REVIEW,
        DisputeStatus.WON,
        DisputeStatus.LOST,
        DisputeStatus.CLOSED,
    }),
    DisputeStatus.UNDER_REVIEW: frozenset({
        DisputeStatus.WON,
        DisputeStatus.LOST,
        DisputeStatus.CLOSED,
    }),
    DisputeStatus.WON: frozenset({DisputeStatus.CLOSED}),
    DisputeStatus.LOST: frozenset({DisputeStatus.CLOSED}),
    DisputeStatus.CLOSED: frozenset(),
}

CHARGEBACK_TRANSITIONS: Mapping[ChargebackStatus, frozenset[ChargebackStatus]] = {
    ChargebackStatus.PENDING: frozenset({
        ChargebackStatus.DEBITED,
        ChargebackStatus.REVERSED,
        ChargebackStatus.FINAL_LOSS,
    }),
    ChargebackStatus.DEBITED: frozenset({
        ChargebackStatus.REVERSED,
        ChargebackStatus.FINAL_LOSS,
    }),
    ChargebackStatus.REVERSED: frozenset(),
    ChargebackStatus.FINAL_LOSS: frozenset(),
}

ADJUSTMENT_TRANSITIONS: Mapping[AdjustmentStatus, frozenset[AdjustmentStatus]] = {
    AdjustmentStatus.PROPOSED: frozenset({
        AdjustmentStatus.APPROVED,
        AdjustmentStatus.REJECTED,
        AdjustmentStatus.CANCELLED,
    }),
    AdjustmentStatus.APPROVED: frozenset({
        AdjustmentStatus.POSTED,
        AdjustmentStatus.CANCELLED,
    }),
    AdjustmentStatus.REJECTED: frozenset(),
    AdjustmentStatus.CANCELLED: frozenset(),
    AdjustmentStatus.POSTED: frozenset(),
}

PROVIDER_OPERATION_TRANSITIONS: Mapping[ProviderOperationStatus, frozenset[ProviderOperationStatus]] = {
    ProviderOperationStatus.RESERVED: frozenset({
        ProviderOperationStatus.IN_PROGRESS,
        ProviderOperationStatus.CANCELLED,
    }),
    ProviderOperationStatus.IN_PROGRESS: frozenset({
        ProviderOperationStatus.UNKNOWN,
        ProviderOperationStatus.SUCCEEDED,
        ProviderOperationStatus.FAILED_RETRYABLE,
        ProviderOperationStatus.FAILED_FINAL,
        ProviderOperationStatus.CANCELLED,
    }),
    ProviderOperationStatus.UNKNOWN: frozenset({
        ProviderOperationStatus.SUCCEEDED,
        ProviderOperationStatus.FAILED_RETRYABLE,
        ProviderOperationStatus.FAILED_FINAL,
        ProviderOperationStatus.CANCELLED,
    }),
    ProviderOperationStatus.SUCCEEDED: frozenset(),
    ProviderOperationStatus.FAILED_RETRYABLE: frozenset(),
    ProviderOperationStatus.FAILED_FINAL: frozenset(),
    ProviderOperationStatus.CANCELLED: frozenset(),
}


StateT = TypeVar("StateT", bound=Enum)


def transition_action(
    current: StateT,
    target: StateT,
    transitions: Mapping[StateT, frozenset[StateT]],
) -> TransitionAction:
    if type(current) is not type(target):
        raise InvalidFinancialTransition(
            f"State-machine type mismatch: {type(current).__name__} -> {type(target).__name__}"
        )
    if current == target:
        return TransitionAction.NOOP
    if target in transitions.get(current, frozenset()):
        return TransitionAction.APPLY
    raise InvalidFinancialTransition(f"Illegal financial transition: {current.value} -> {target.value}")


CURRENCY_MINOR_UNIT_EXPONENTS: Mapping[str, int] = {
    # PAY-1 production currency allowlist. Additional currencies require an
    # explicit reviewed contract + migration/readiness phase.
    "INR": 2,
}


def _currency_quantum(currency_code: str) -> Decimal:
    exponent = CURRENCY_MINOR_UNIT_EXPONENTS.get(currency_code)
    if exponent is None:
        raise MoneyValidationError(f"Unsupported production currency: {currency_code!r}")
    return Decimal(1).scaleb(-exponent)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise MoneyValidationError("Money amount must be Decimal; float/int/string coercion is forbidden")
        if not self.amount.is_finite():
            raise MoneyValidationError("Money amount must be finite")
        if not isinstance(self.currency_code, str):
            raise MoneyValidationError("Currency code must be a string")
        code = self.currency_code.strip().upper()
        if code != self.currency_code or len(code) != 3 or not code.isalpha():
            raise MoneyValidationError("Currency code must be canonical uppercase ISO-4217 form")
        quantum = _currency_quantum(code)
        if self.amount != self.amount.quantize(quantum):
            raise MoneyValidationError(
                f"{code} amount has precision beyond the configured minor-unit exponent"
            )

    @property
    def minor_units(self) -> int:
        exponent = CURRENCY_MINOR_UNIT_EXPONENTS[self.currency_code]
        scale = Decimal(10) ** exponent
        return int((self.amount * scale).to_integral_exact())

    @classmethod
    def from_minor_units(cls, minor_units: int, currency_code: str) -> "Money":
        if isinstance(minor_units, bool) or not isinstance(minor_units, int):
            raise MoneyValidationError("Provider minor units must be an integer")
        code = currency_code.strip().upper()
        exponent = CURRENCY_MINOR_UNIT_EXPONENTS.get(code)
        if exponent is None:
            raise MoneyValidationError(f"Unsupported production currency: {currency_code!r}")
        amount = Decimal(minor_units) / (Decimal(10) ** exponent)
        return cls(amount=amount, currency_code=code)

    def require_positive(self, *, label: str = "Amount") -> "Money":
        if self.amount <= 0:
            raise MoneyValidationError(f"{label} must be positive")
        return self

    def require_nonnegative(self, *, label: str = "Amount") -> "Money":
        if self.amount < 0:
            raise MoneyValidationError(f"{label} must be non-negative")
        return self

    def require_same_currency(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise MoneyValidationError("Currency comparison requires another Money value")
        if self.currency_code != other.currency_code:
            raise MoneyValidationError(
                f"Currency mismatch: {self.currency_code} != {other.currency_code}"
            )


CURRENT_PAYMENT_STATUS_COMPATIBILITY: Mapping[str, tuple[PaymentStatus, str]] = {
    "created": (PaymentStatus.CREATED, "direct"),
    "pending": (PaymentStatus.PENDING, "direct"),
    "authorized": (PaymentStatus.AUTHORIZED, "direct"),
    "captured": (PaymentStatus.CAPTURED, "direct"),
    "failed": (PaymentStatus.FAILED, "direct"),
    "cancelled": (PaymentStatus.CANCELLED, "direct"),
    # Current Finance Core overloads these concepts into payment.status.
    # PAY-1 freezes the target as captured acquisition truth + separate
    # settlement/refund projections.
    "settled": (PaymentStatus.CAPTURED, "derive_settlement_separately"),
    "partially_refunded": (PaymentStatus.CAPTURED, "derive_refund_projection_separately"),
    "refunded": (PaymentStatus.CAPTURED, "derive_refund_projection_separately"),
}

CURRENT_INVOICE_STATUS_COMPATIBILITY: Mapping[str, tuple[InvoiceStatus | None, str]] = {
    "draft": (InvoiceStatus.DRAFT, "direct"),
    "issued": (InvoiceStatus.ISSUED, "direct"),
    "partially_paid": (InvoiceStatus.PARTIALLY_PAID, "direct"),
    "paid": (InvoiceStatus.PAID, "direct"),
    "cancelled": (InvoiceStatus.CANCELLED, "direct"),
    "voided": (InvoiceStatus.VOIDED, "direct"),
    "credited": (InvoiceStatus.CREDITED, "direct"),
    "overdue": (None, "derive_due_state_from_due_date_and_outstanding_balance"),
}

CURRENT_PLATFORM_PROVIDER_OPERATION_COMPATIBILITY: Mapping[str, tuple[ProviderOperationStatus | None, str]] = {
    "reserved": (ProviderOperationStatus.RESERVED, "direct"),
    "in_progress": (ProviderOperationStatus.IN_PROGRESS, "direct"),
    "succeeded": (ProviderOperationStatus.SUCCEEDED, "direct"),
    "unknown": (ProviderOperationStatus.UNKNOWN, "direct"),
    # Existing platform rows do not distinguish retryable from final failure.
    # PAY-2/PAY-11 migration must classify from authoritative evidence rather
    # than inventing retryability.
    "failed": (None, "requires_evidence_classification"),
}


PAY1_HARD_INVARIANTS = frozenset({
    "captured_payment_acquisition_truth_is_not_rewritten_by_settlement_refund_dispute_or_chargeback",
    "payment_attempt_unknown_requires_reconciliation_before_replacement_effect",
    "provider_event_staleness_requires_authoritative_version_or_time_evidence_not_enum_order",
    "issued_statutory_documents_are_immutable_except_controlled_status_or_linked_corrective_documents",
    "payment_allocation_and_reversal_history_is_append_only",
    "settlement_reversal_is_a_new_linked_settlement_not_mutation_of_reconciled_truth",
    "refund_execution_is_separate_from_refund_business_intent",
    "dispute_and_chargeback_do_not_rewrite_original_capture_truth",
    "manual_adjustment_posting_requires_approved_command_and_append_only_ledger_effect",
    "browser_redis_celery_telemetry_and_operator_input_are_not_financial_authority",
    "money_uses_decimal_business_units_and_integer_provider_minor_units_never_float",
    "cross_currency_allocation_refund_credit_or_settlement_is_forbidden",
    "provider_level_refund_total_cannot_exceed_captured_amount",
    "allocation_reversal_total_cannot_exceed_original_allocation",
    "financial_history_is_not_hard_deleted",
})
