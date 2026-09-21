from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.platform_billing.domain.accounting_reconciliation import (
    MISMATCH_CATEGORIES,
    SAFE_OUTCOMES,
    FinancialObservation,
    MismatchCategory,
    ReconciliationObjectType,
    SafeOutcome,
    ThreeWayReconciliationInput,
    classify_three_way_reconciliation,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _obs(
    side: str,
    *,
    object_type: str = "captured_payment",
    amount_minor: int | None = 11800,
    currency_code: str | None = "INR",
    status: str | None = "succeeded",
    fee_minor: int | None = 200,
    authoritative: bool = True,
    sha: str = SHA_A,
) -> FinancialObservation:
    return FinancialObservation(
        side=side,
        object_type=object_type,
        object_ref=f"{side}-{object_type}-1",
        amount_minor=amount_minor,
        currency_code=currency_code,
        status=status,
        fee_minor=fee_minor,
        evidence_sha256=sha,
        evidence_ref=f"evidence://pay14/{side}/{object_type}",
        observed_at=T0,
        authoritative=authoritative,
    )


def _input(
    *,
    object_type: str = "captured_payment",
    local: FinancialObservation | None = None,
    provider: FinancialObservation | None = None,
    settlement: FinancialObservation | None = None,
    provider_duplicate_count: int = 1,
    provider_object_known: bool = True,
    settlement_expected: bool = True,
) -> ThreeWayReconciliationInput:
    return ThreeWayReconciliationInput(
        object_type=object_type,
        local=local,
        provider=provider,
        settlement=settlement,
        provider_duplicate_count=provider_duplicate_count,
        provider_object_known=provider_object_known,
        settlement_expected=settlement_expected,
    )


def test_pay14_taxonomies_are_exact():
    assert MISMATCH_CATEGORIES == {
        "provider_only",
        "local_only",
        "amount_mismatch",
        "currency_mismatch",
        "status_mismatch",
        "settlement_missing",
        "duplicate_provider_object",
        "unknown_provider_object",
        "refund_mismatch",
        "fee_mismatch",
    }
    assert SAFE_OUTCOMES == {
        "auto_resolved_by_authoritative_evidence",
        "retry_required",
        "manual_review_required",
        "security_incident",
        "accounting_incident",
    }
    assert {item.value for item in ReconciliationObjectType} == {
        "captured_payment",
        "settlement",
        "gateway_fee",
        "refund",
        "refund_fee",
        "dispute",
        "chargeback",
        "adjustment",
    }


def test_authoritative_three_way_match_auto_resolves_reconciliation_only():
    decision = classify_three_way_reconciliation(
        _input(
            local=_obs("local", sha=SHA_A),
            provider=_obs("provider", sha=SHA_B),
            settlement=_obs("settlement", sha=SHA_C),
        )
    )
    assert decision.mismatch_category is None
    assert (
        decision.safe_outcome
        == SafeOutcome.auto_resolved_by_authoritative_evidence.value
    )
    assert decision.authoritative_evidence_complete is True
    assert decision.auto_financial_mutation_allowed is False


def test_non_authoritative_match_requires_manual_review():
    decision = classify_three_way_reconciliation(
        _input(
            local=_obs("local", sha=SHA_A),
            provider=_obs("provider", authoritative=False, sha=SHA_B),
            settlement=_obs("settlement", sha=SHA_C),
        )
    )
    assert decision.mismatch_category is None
    assert decision.safe_outcome == SafeOutcome.manual_review_required.value
    assert decision.auto_financial_mutation_allowed is False


@pytest.mark.parametrize(
    ("data", "category", "outcome"),
    [
        (
            _input(provider=_obs("provider")),
            MismatchCategory.provider_only.value,
            SafeOutcome.manual_review_required.value,
        ),
        (
            _input(local=_obs("local")),
            MismatchCategory.local_only.value,
            SafeOutcome.manual_review_required.value,
        ),
        (
            _input(
                local=_obs("local", amount_minor=11800),
                provider=_obs("provider", amount_minor=11799),
                settlement=_obs("settlement", amount_minor=11800),
            ),
            MismatchCategory.amount_mismatch.value,
            SafeOutcome.accounting_incident.value,
        ),
        (
            _input(
                local=_obs("local", currency_code="INR"),
                provider=_obs("provider", currency_code="USD"),
                settlement=_obs("settlement", currency_code="INR"),
            ),
            MismatchCategory.currency_mismatch.value,
            SafeOutcome.accounting_incident.value,
        ),
        (
            _input(
                local=_obs("local", status="succeeded"),
                provider=_obs("provider", status="failed"),
                settlement=_obs("settlement", status="succeeded"),
            ),
            MismatchCategory.status_mismatch.value,
            SafeOutcome.accounting_incident.value,
        ),
        (
            _input(
                local=_obs("local"),
                provider=_obs("provider"),
                settlement=None,
            ),
            MismatchCategory.settlement_missing.value,
            SafeOutcome.retry_required.value,
        ),
        (
            _input(
                local=_obs("local"),
                provider=_obs("provider"),
                settlement=_obs("settlement"),
                provider_duplicate_count=2,
            ),
            MismatchCategory.duplicate_provider_object.value,
            SafeOutcome.security_incident.value,
        ),
        (
            _input(
                local=None,
                provider=_obs("provider"),
                settlement=None,
                provider_object_known=False,
            ),
            MismatchCategory.unknown_provider_object.value,
            SafeOutcome.manual_review_required.value,
        ),
        (
            _input(
                object_type="refund",
                local=_obs("local", object_type="refund", amount_minor=5000),
                provider=_obs("provider", object_type="refund", amount_minor=4900),
                settlement=_obs("settlement", object_type="refund", amount_minor=5000),
            ),
            MismatchCategory.refund_mismatch.value,
            SafeOutcome.accounting_incident.value,
        ),
        (
            _input(
                object_type="gateway_fee",
                local=_obs("local", object_type="gateway_fee", amount_minor=200),
                provider=_obs("provider", object_type="gateway_fee", amount_minor=250),
                settlement=_obs("settlement", object_type="gateway_fee", amount_minor=200),
            ),
            MismatchCategory.fee_mismatch.value,
            SafeOutcome.accounting_incident.value,
        ),
    ],
)
def test_every_explicit_mismatch_category_is_fail_closed(data, category, outcome):
    decision = classify_three_way_reconciliation(data)
    assert decision.mismatch_category == category
    assert decision.safe_outcome == outcome
    assert decision.auto_financial_mutation_allowed is False


def test_refund_status_and_currency_mismatch_stay_refund_specific():
    for provider in (
        _obs("provider", object_type="refund", status="failed"),
        _obs("provider", object_type="refund", currency_code="USD"),
    ):
        decision = classify_three_way_reconciliation(
            _input(
                object_type="refund",
                local=_obs("local", object_type="refund"),
                provider=provider,
                settlement=_obs("settlement", object_type="refund"),
            )
        )
        assert decision.mismatch_category == "refund_mismatch"
        assert decision.safe_outcome == "accounting_incident"


def test_fee_value_mismatch_is_fee_specific():
    decision = classify_three_way_reconciliation(
        _input(
            object_type="refund_fee",
            local=_obs(
                "local",
                object_type="refund_fee",
                amount_minor=100,
                fee_minor=10,
            ),
            provider=_obs(
                "provider",
                object_type="refund_fee",
                amount_minor=100,
                fee_minor=11,
            ),
            settlement=_obs(
                "settlement",
                object_type="refund_fee",
                amount_minor=100,
                fee_minor=10,
            ),
        )
    )
    assert decision.mismatch_category == "fee_mismatch"
    assert decision.safe_outcome == "accounting_incident"


def test_observation_validation_requires_aware_durable_evidence():
    bad = FinancialObservation(
        side="provider",
        object_type="captured_payment",
        object_ref="pay_bad",
        amount_minor=100,
        currency_code="INR",
        status="succeeded",
        fee_minor=0,
        evidence_sha256="x" * 64,
        evidence_ref="evidence://bad",
        observed_at=datetime(2026, 9, 21, 12, 0),
        authoritative=True,
    )
    with pytest.raises(ValueError):
        bad.validate()


def test_reconciliation_never_exposes_money_mutation_decision():
    scenarios = [
        _input(
            local=_obs("local"),
            provider=_obs("provider"),
            settlement=_obs("settlement"),
        ),
        _input(local=_obs("local"), provider=_obs("provider"), settlement=None),
        _input(provider=_obs("provider")),
        _input(
            local=_obs("local", amount_minor=100),
            provider=_obs("provider", amount_minor=101),
            settlement=_obs("settlement", amount_minor=100),
        ),
    ]
    assert all(
        classify_three_way_reconciliation(item).auto_financial_mutation_allowed
        is False
        for item in scenarios
    )
