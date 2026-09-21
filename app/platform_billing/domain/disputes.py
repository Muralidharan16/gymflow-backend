from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DisputeStatus(str, Enum):
    opened = "opened"
    evidence_required = "evidence_required"
    submitted = "submitted"
    under_review = "under_review"
    won = "won"
    lost = "lost"
    closed = "closed"


class DisputeType(str, Enum):
    chargeback = "chargeback"
    cardholder_dispute = "cardholder_dispute"
    duplicate_charge_allegation = "duplicate_charge_allegation"
    fraud_review = "fraud_review"


class FinancialExceptionType(str, Enum):
    accidental_duplicate_provider_payment = "accidental_duplicate_provider_payment"
    orphan_provider_payment = "orphan_provider_payment"
    orphan_settlement = "orphan_settlement"
    unknown_refund = "unknown_refund"
    wrong_customer_mapping = "wrong_customer_mapping"


class FinancialExceptionStatus(str, Enum):
    detected = "detected"
    quarantined = "quarantined"
    investigating = "investigating"
    mapped = "mapped"
    resolved = "resolved"
    ignored = "ignored"


class DisputeFinancialEntryType(str, Enum):
    liability_recognized = "liability_recognized"
    liability_reversed = "liability_reversed"
    loss_recognized = "loss_recognized"
    loss_reversed = "loss_reversed"


DISPUTE_TRANSITIONS = frozenset(
    {
        ("opened", "evidence_required"),
        ("opened", "submitted"),
        ("opened", "under_review"),
        ("opened", "won"),
        ("opened", "lost"),
        ("evidence_required", "submitted"),
        ("evidence_required", "closed"),
        ("submitted", "under_review"),
        ("submitted", "won"),
        ("submitted", "lost"),
        ("under_review", "won"),
        ("under_review", "lost"),
        ("won", "closed"),
        ("lost", "closed"),
    }
)

EXCEPTION_TRANSITIONS = frozenset(
    {
        ("detected", "quarantined"),
        ("detected", "investigating"),
        ("detected", "mapped"),
        ("detected", "resolved"),
        ("detected", "ignored"),
        ("quarantined", "investigating"),
        ("quarantined", "mapped"),
        ("quarantined", "resolved"),
        ("quarantined", "ignored"),
        ("investigating", "mapped"),
        ("investigating", "resolved"),
        ("investigating", "ignored"),
        ("mapped", "resolved"),
        ("mapped", "ignored"),
    }
)

DISPUTE_OPEN_FACTS = frozenset(
    {
        "chargeback_opened",
        "cardholder_dispute_opened",
        "duplicate_charge_allegation",
        "fraud_review_opened",
    }
)

EXCEPTION_FACTS = frozenset(item.value for item in FinancialExceptionType)


def validate_dispute_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current == DisputeStatus.closed.value:
        raise ValueError("Closed dispute is terminal")
    if (current, target) not in DISPUTE_TRANSITIONS:
        raise ValueError(f"Forbidden dispute transition: {current!r} -> {target!r}")
    return True


def validate_exception_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current in {
        FinancialExceptionStatus.resolved.value,
        FinancialExceptionStatus.ignored.value,
    }:
        raise ValueError("Resolved/ignored financial exception is terminal")
    if (current, target) not in EXCEPTION_TRANSITIONS:
        raise ValueError(
            f"Forbidden financial exception transition: {current!r} -> {target!r}"
        )
    return True


@dataclass(frozen=True)
class CapturedPaymentTruth:
    payment_attempt_id: str
    invoice_id: str
    organization_id: str
    status: str
    amount_minor: int
    currency_code: str
    provider_code: str
    environment: str
    external_payment_ref: str | None

    def require_captured(self) -> None:
        if self.status != "succeeded":
            raise ValueError(
                "PAY-13 disputes may attach only to historically captured payments"
            )
        if self.amount_minor <= 0:
            raise ValueError("Captured payment amount must be positive")


@dataclass(frozen=True)
class NormalizedFinancialFact:
    fact_type: str
    provider_code: str
    environment: str
    external_object_ref: str
    amount_minor: int | None
    currency_code: str | None
    evidence_sha256: str
    evidence_ref: str
    observed_at: datetime
    provider_decision_ref: str | None = None
    dispute_type: str | None = None
    source_type: str = "webhook"
    source_id: str | None = None

    def validate(self) -> None:
        if self.environment not in {"test", "live"}:
            raise ValueError("Provider environment must be test or live")
        if not self.provider_code or not self.external_object_ref:
            raise ValueError("Provider code and object reference are required")
        if len(self.evidence_sha256) != 64:
            raise ValueError("Provider evidence SHA-256 must be 64 hex characters")
        try:
            int(self.evidence_sha256, 16)
        except ValueError as exc:
            raise ValueError("Provider evidence SHA-256 must be hexadecimal") from exc
        if self.source_type not in {"webhook", "reconciliation", "manual_review"}:
            raise ValueError("Unsupported PAY-13 source type")
        if self.amount_minor is not None and self.amount_minor <= 0:
            raise ValueError("Financial fact amount must be positive")
        if self.currency_code is not None and (
            len(self.currency_code) != 3 or self.currency_code != self.currency_code.upper()
        ):
            raise ValueError("Currency code must be ISO-style uppercase CHAR(3)")


@dataclass(frozen=True)
class DisputeSnapshot:
    status: str
    dispute_type: str
    amount_minor: int
    currency_code: str
    financial_hold_active: bool


@dataclass(frozen=True)
class DisputePlan:
    action: str
    target_status: str | None
    freeze_financial_actions: bool
    financial_entry_type: str | None
    preserve_payment_status: bool = True
    notification_types: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str = ""


@dataclass(frozen=True)
class FinancialExceptionPlan:
    status: str
    quarantine: bool
    manual_review_required: bool
    automatic_financial_mutation_allowed: bool
    reason_code: str


def plan_financial_fact(
    *,
    fact: NormalizedFinancialFact,
    payment: CapturedPaymentTruth | None,
    dispute: DisputeSnapshot | None,
) -> DisputePlan | FinancialExceptionPlan:
    """
    Convert a normalized provider/reconciliation fact into a PAY-13 domain plan.

    Provider facts never rewrite the historical payment result. A captured payment
    stays captured; a later dispute/chargeback is represented by a separate
    aggregate and append-only financial entries.
    """
    fact.validate()

    if fact.fact_type in EXCEPTION_FACTS:
        return FinancialExceptionPlan(
            status=FinancialExceptionStatus.quarantined.value,
            quarantine=True,
            manual_review_required=True,
            automatic_financial_mutation_allowed=False,
            reason_code=f"PAY13_{fact.fact_type.upper()}",
        )

    if fact.fact_type in DISPUTE_OPEN_FACTS:
        if payment is None:
            return FinancialExceptionPlan(
                status=FinancialExceptionStatus.quarantined.value,
                quarantine=True,
                manual_review_required=True,
                automatic_financial_mutation_allowed=False,
                reason_code="PAY13_UNMAPPED_DISPUTE",
            )
        payment.require_captured()
        if fact.amount_minor is None or fact.currency_code is None:
            raise ValueError("Dispute-opening fact requires amount and currency")
        if fact.currency_code != payment.currency_code:
            raise ValueError("Dispute currency must match captured payment")
        if fact.amount_minor > payment.amount_minor:
            raise ValueError("Dispute amount cannot exceed captured payment amount")
        if dispute is not None:
            return DisputePlan(
                action="dedupe_existing_dispute",
                target_status=None,
                freeze_financial_actions=dispute.financial_hold_active,
                financial_entry_type=None,
                reason_code="PAY13_REPLAY",
            )
        return DisputePlan(
            action="open_dispute",
            target_status=DisputeStatus.opened.value,
            freeze_financial_actions=True,
            financial_entry_type=DisputeFinancialEntryType.liability_recognized.value,
            notification_types=("dispute_opened",),
            reason_code="PAY13_DISPUTE_OPENED",
        )

    if dispute is None:
        return FinancialExceptionPlan(
            status=FinancialExceptionStatus.quarantined.value,
            quarantine=True,
            manual_review_required=True,
            automatic_financial_mutation_allowed=False,
            reason_code="PAY13_ORPHAN_DISPUTE_UPDATE",
        )

    if fact.amount_minor is not None and fact.amount_minor != dispute.amount_minor:
        raise ValueError("Dispute update amount must match dispute amount")
    if fact.currency_code is not None and fact.currency_code != dispute.currency_code:
        raise ValueError("Dispute update currency must match dispute currency")

    if fact.fact_type == "evidence_required":
        validate_dispute_transition(dispute.status, DisputeStatus.evidence_required.value)
        return DisputePlan(
            action="transition",
            target_status=DisputeStatus.evidence_required.value,
            freeze_financial_actions=True,
            financial_entry_type=None,
            notification_types=("dispute_evidence_required",),
            reason_code="PAY13_EVIDENCE_REQUIRED",
        )

    if fact.fact_type == "evidence_submitted":
        validate_dispute_transition(dispute.status, DisputeStatus.submitted.value)
        return DisputePlan(
            action="transition",
            target_status=DisputeStatus.submitted.value,
            freeze_financial_actions=True,
            financial_entry_type=None,
            notification_types=("dispute_evidence_submitted",),
            reason_code="PAY13_EVIDENCE_SUBMITTED",
        )

    if fact.fact_type == "under_review":
        validate_dispute_transition(dispute.status, DisputeStatus.under_review.value)
        return DisputePlan(
            action="transition",
            target_status=DisputeStatus.under_review.value,
            freeze_financial_actions=True,
            financial_entry_type=None,
            reason_code="PAY13_UNDER_REVIEW",
        )

    if fact.fact_type == "dispute_won":
        validate_dispute_transition(dispute.status, DisputeStatus.won.value)
        if not fact.provider_decision_ref:
            raise ValueError("Won dispute requires provider decision reference")
        return DisputePlan(
            action="provider_decision",
            target_status=DisputeStatus.won.value,
            freeze_financial_actions=False,
            financial_entry_type=DisputeFinancialEntryType.liability_reversed.value,
            notification_types=("dispute_won",),
            reason_code="PAY13_DISPUTE_WON",
        )

    if fact.fact_type == "dispute_lost":
        validate_dispute_transition(dispute.status, DisputeStatus.lost.value)
        if not fact.provider_decision_ref:
            raise ValueError("Lost dispute requires provider decision reference")
        return DisputePlan(
            action="provider_decision",
            target_status=DisputeStatus.lost.value,
            freeze_financial_actions=False,
            financial_entry_type=DisputeFinancialEntryType.loss_recognized.value,
            notification_types=("dispute_lost",),
            reason_code="PAY13_DISPUTE_LOST",
        )

    if fact.fact_type == "chargeback_reversed":
        if dispute.status not in {
            DisputeStatus.lost.value,
            DisputeStatus.closed.value,
        }:
            raise ValueError(
                "Chargeback reversal is valid only after a recorded loss/closed dispute"
            )
        return DisputePlan(
            action="record_chargeback_reversal",
            target_status=None,
            freeze_financial_actions=False,
            financial_entry_type=DisputeFinancialEntryType.loss_reversed.value,
            notification_types=("chargeback_reversed",),
            reason_code="PAY13_CHARGEBACK_REVERSED",
        )

    raise ValueError(f"Unsupported PAY-13 financial fact: {fact.fact_type!r}")
