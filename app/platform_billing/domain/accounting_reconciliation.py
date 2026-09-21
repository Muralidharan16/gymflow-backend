from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable


class ReconciliationObjectType(str, Enum):
    captured_payment = "captured_payment"
    settlement = "settlement"
    gateway_fee = "gateway_fee"
    refund = "refund"
    refund_fee = "refund_fee"
    dispute = "dispute"
    chargeback = "chargeback"
    adjustment = "adjustment"


class MismatchCategory(str, Enum):
    provider_only = "provider_only"
    local_only = "local_only"
    amount_mismatch = "amount_mismatch"
    currency_mismatch = "currency_mismatch"
    status_mismatch = "status_mismatch"
    settlement_missing = "settlement_missing"
    duplicate_provider_object = "duplicate_provider_object"
    unknown_provider_object = "unknown_provider_object"
    refund_mismatch = "refund_mismatch"
    fee_mismatch = "fee_mismatch"


class SafeOutcome(str, Enum):
    auto_resolved_by_authoritative_evidence = "auto_resolved_by_authoritative_evidence"
    retry_required = "retry_required"
    manual_review_required = "manual_review_required"
    security_incident = "security_incident"
    accounting_incident = "accounting_incident"


SUPPORTED_OBJECT_TYPES = frozenset(item.value for item in ReconciliationObjectType)
MISMATCH_CATEGORIES = frozenset(item.value for item in MismatchCategory)
SAFE_OUTCOMES = frozenset(item.value for item in SafeOutcome)


@dataclass(frozen=True)
class FinancialObservation:
    side: str
    object_type: str
    object_ref: str
    amount_minor: int | None
    currency_code: str | None
    status: str | None
    fee_minor: int | None
    evidence_sha256: str
    evidence_ref: str
    observed_at: datetime
    authoritative: bool

    def validate(self) -> None:
        if self.side not in {"local", "provider", "settlement"}:
            raise ValueError("PAY-14 observation side must be local/provider/settlement")
        if self.object_type not in SUPPORTED_OBJECT_TYPES:
            raise ValueError("PAY-14 unsupported reconciliation object type")
        if not self.object_ref.strip():
            raise ValueError("PAY-14 observation requires object reference")
        if self.amount_minor is not None and self.amount_minor < 0:
            raise ValueError("PAY-14 observation amount cannot be negative")
        if self.fee_minor is not None and self.fee_minor < 0:
            raise ValueError("PAY-14 observation fee cannot be negative")
        if self.currency_code is not None:
            if len(self.currency_code) != 3 or self.currency_code != self.currency_code.upper():
                raise ValueError("PAY-14 currency must be uppercase ISO-style CHAR(3)")
        if len(self.evidence_sha256) != 64:
            raise ValueError("PAY-14 evidence SHA-256 must be 64 hex characters")
        try:
            int(self.evidence_sha256, 16)
        except ValueError as exc:
            raise ValueError("PAY-14 evidence SHA-256 must be hexadecimal") from exc
        if not self.evidence_ref.strip():
            raise ValueError("PAY-14 evidence reference is required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("PAY-14 evidence observed_at must be timezone-aware")


@dataclass(frozen=True)
class ThreeWayReconciliationInput:
    object_type: str
    local: FinancialObservation | None
    provider: FinancialObservation | None
    settlement: FinancialObservation | None
    provider_duplicate_count: int = 1
    provider_object_known: bool = True
    settlement_expected: bool = True

    def validate(self) -> None:
        if self.object_type not in SUPPORTED_OBJECT_TYPES:
            raise ValueError("PAY-14 unsupported reconciliation object type")
        if self.provider_duplicate_count < 0:
            raise ValueError("PAY-14 provider duplicate count cannot be negative")
        for observation in (self.local, self.provider, self.settlement):
            if observation is None:
                continue
            observation.validate()
            if observation.object_type != self.object_type:
                raise ValueError("PAY-14 three-way observation object types must agree")


@dataclass(frozen=True)
class ReconciliationDecision:
    mismatch_category: str | None
    safe_outcome: str
    auto_financial_mutation_allowed: bool
    authoritative_evidence_complete: bool
    reason_code: str


def _first_non_null(values: Iterable[str | None]) -> str | None:
    for value in values:
        if value is not None:
            return value
    return None


def classify_three_way_reconciliation(
    data: ThreeWayReconciliationInput,
) -> ReconciliationDecision:
    """
    Classify a PAY-14 three-way reconciliation result.

    This function returns a decision only. It never proposes or applies a money
    mutation. Even authoritative evidence can auto-resolve only the
    reconciliation *item*, never rewrite payment/refund/dispute financial truth.
    """
    data.validate()
    local = data.local
    provider = data.provider
    settlement = data.settlement

    if not data.provider_object_known and provider is not None:
        return _decision(
            MismatchCategory.unknown_provider_object.value,
            SafeOutcome.manual_review_required.value,
            False,
            "PAY14_UNKNOWN_PROVIDER_OBJECT",
        )

    if data.provider_duplicate_count > 1:
        return _decision(
            MismatchCategory.duplicate_provider_object.value,
            SafeOutcome.security_incident.value,
            False,
            "PAY14_DUPLICATE_PROVIDER_OBJECT",
        )

    if local is None and provider is not None:
        return _decision(
            MismatchCategory.provider_only.value,
            SafeOutcome.manual_review_required.value,
            False,
            "PAY14_PROVIDER_ONLY",
        )

    if local is not None and provider is None:
        return _decision(
            MismatchCategory.local_only.value,
            SafeOutcome.manual_review_required.value,
            False,
            "PAY14_LOCAL_ONLY",
        )

    if local is None and provider is None:
        if settlement is not None:
            return _decision(
                MismatchCategory.unknown_provider_object.value,
                SafeOutcome.manual_review_required.value,
                False,
                "PAY14_SETTLEMENT_WITHOUT_MAPPED_OBJECT",
            )
        raise ValueError("PAY-14 reconciliation requires at least one observation")

    assert local is not None and provider is not None

    currencies = {
        value
        for value in (
            local.currency_code,
            provider.currency_code,
            settlement.currency_code if settlement is not None else None,
        )
        if value is not None
    }
    if len(currencies) > 1:
        category = (
            MismatchCategory.refund_mismatch.value
            if data.object_type == ReconciliationObjectType.refund.value
            else MismatchCategory.currency_mismatch.value
        )
        return _decision(
            category,
            SafeOutcome.accounting_incident.value,
            False,
            "PAY14_CURRENCY_MISMATCH",
        )

    amounts = {
        value
        for value in (
            local.amount_minor,
            provider.amount_minor,
            settlement.amount_minor if settlement is not None else None,
        )
        if value is not None
    }
    if len(amounts) > 1:
        category = (
            MismatchCategory.refund_mismatch.value
            if data.object_type == ReconciliationObjectType.refund.value
            else (
                MismatchCategory.fee_mismatch.value
                if data.object_type
                in {
                    ReconciliationObjectType.gateway_fee.value,
                    ReconciliationObjectType.refund_fee.value,
                }
                else MismatchCategory.amount_mismatch.value
            )
        )
        return _decision(
            category,
            SafeOutcome.accounting_incident.value,
            False,
            "PAY14_AMOUNT_MISMATCH",
        )

    if (
        local.fee_minor is not None
        and provider.fee_minor is not None
        and local.fee_minor != provider.fee_minor
    ):
        return _decision(
            MismatchCategory.fee_mismatch.value,
            SafeOutcome.accounting_incident.value,
            False,
            "PAY14_FEE_MISMATCH",
        )

    local_status = local.status
    provider_status = provider.status
    if (
        local_status is not None
        and provider_status is not None
        and local_status != provider_status
    ):
        category = (
            MismatchCategory.refund_mismatch.value
            if data.object_type == ReconciliationObjectType.refund.value
            else MismatchCategory.status_mismatch.value
        )
        return _decision(
            category,
            SafeOutcome.accounting_incident.value,
            False,
            "PAY14_STATUS_MISMATCH",
        )

    if data.settlement_expected and settlement is None:
        return _decision(
            MismatchCategory.settlement_missing.value,
            SafeOutcome.retry_required.value,
            False,
            "PAY14_SETTLEMENT_MISSING",
        )

    authoritative = all(
        observation.authoritative
        for observation in (local, provider, settlement)
        if observation is not None
    )
    if not authoritative:
        return _decision(
            None,
            SafeOutcome.manual_review_required.value,
            False,
            "PAY14_EVIDENCE_NOT_AUTHORITATIVE",
            authoritative=False,
        )

    return _decision(
        None,
        SafeOutcome.auto_resolved_by_authoritative_evidence.value,
        False,
        "PAY14_THREE_WAY_MATCH",
        authoritative=True,
    )


def _decision(
    mismatch_category: str | None,
    safe_outcome: str,
    auto_financial_mutation_allowed: bool,
    reason_code: str,
    *,
    authoritative: bool = False,
) -> ReconciliationDecision:
    return ReconciliationDecision(
        mismatch_category=mismatch_category,
        safe_outcome=safe_outcome,
        auto_financial_mutation_allowed=auto_financial_mutation_allowed,
        authoritative_evidence_complete=authoritative,
        reason_code=reason_code,
    )


def reconciliation_key(
    *,
    provider_code: str,
    environment: str,
    object_type: str,
    external_object_ref: str,
) -> str:
    if not provider_code or not external_object_ref:
        raise ValueError("PAY-14 reconciliation key requires provider and external reference")
    return "|".join((provider_code, environment, object_type, external_object_ref))
