from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.platform_billing.domain.disputes import (
    CapturedPaymentTruth,
    DisputeSnapshot,
    FinancialExceptionPlan,
    NormalizedFinancialFact,
    plan_financial_fact,
    validate_dispute_transition,
    validate_exception_transition,
)
from app.platform_billing.services.disputes import FinancialExceptionPlannerService


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SHA_A = "a" * 64


def _payment() -> CapturedPaymentTruth:
    return CapturedPaymentTruth(
        payment_attempt_id="payment-1",
        invoice_id="invoice-1",
        organization_id="org-1",
        status="succeeded",
        amount_minor=11800,
        currency_code="INR",
        provider_code="fake",
        environment="test",
        external_payment_ref="pay_test_1",
    )


def _fact(
    fact_type: str,
    *,
    amount_minor: int | None = 11800,
    currency_code: str | None = "INR",
    decision_ref: str | None = None,
) -> NormalizedFinancialFact:
    return NormalizedFinancialFact(
        fact_type=fact_type,
        provider_code="fake",
        environment="test",
        external_object_ref="dp_test_1",
        amount_minor=amount_minor,
        currency_code=currency_code,
        evidence_sha256=SHA_A,
        evidence_ref=f"evidence://pay13/{fact_type}",
        observed_at=T0,
        provider_decision_ref=decision_ref,
        dispute_type="chargeback",
    )


def test_dispute_state_machine_is_forward_only():
    for current, target in (
        ("opened", "evidence_required"),
        ("evidence_required", "submitted"),
        ("submitted", "under_review"),
        ("under_review", "won"),
        ("won", "closed"),
    ):
        assert validate_dispute_transition(current, target)

    with pytest.raises(ValueError, match="Forbidden"):
        validate_dispute_transition("under_review", "opened")
    with pytest.raises(ValueError, match="terminal"):
        validate_dispute_transition("closed", "opened")


def test_exception_state_machine_is_manual_and_terminal():
    assert validate_exception_transition("detected", "quarantined")
    assert validate_exception_transition("quarantined", "investigating")
    assert validate_exception_transition("investigating", "mapped")
    assert validate_exception_transition("mapped", "resolved")
    with pytest.raises(ValueError, match="terminal"):
        validate_exception_transition("resolved", "investigating")


def test_chargeback_opens_separate_liability_without_rewriting_payment():
    result = FinancialExceptionPlannerService().plan(
        fact=_fact("chargeback_opened"),
        payment=_payment(),
        dispute=None,
    )
    plan = result.plan
    assert plan.action == "open_dispute"
    assert plan.target_status == "opened"
    assert plan.freeze_financial_actions is True
    assert plan.financial_entry_type == "liability_recognized"
    assert plan.preserve_payment_status is True
    assert result.payment_status_rewrite is None


def test_dispute_requires_historically_captured_payment():
    failed_payment = CapturedPaymentTruth(
        **{**_payment().__dict__, "status": "failed"}
    )
    with pytest.raises(ValueError, match="historically captured"):
        plan_financial_fact(
            fact=_fact("chargeback_opened"),
            payment=failed_payment,
            dispute=None,
        )


def test_dispute_amount_and_currency_are_bound_to_capture_truth():
    with pytest.raises(ValueError, match="cannot exceed"):
        plan_financial_fact(
            fact=_fact("chargeback_opened", amount_minor=11801),
            payment=_payment(),
            dispute=None,
        )
    with pytest.raises(ValueError, match="currency"):
        plan_financial_fact(
            fact=_fact("chargeback_opened", currency_code="USD"),
            payment=_payment(),
            dispute=None,
        )


def test_unmapped_dispute_is_quarantined_not_guessed():
    plan = plan_financial_fact(
        fact=_fact("chargeback_opened"),
        payment=None,
        dispute=None,
    )
    assert isinstance(plan, FinancialExceptionPlan)
    assert plan.status == "quarantined"
    assert plan.manual_review_required is True
    assert plan.automatic_financial_mutation_allowed is False
    assert plan.reason_code == "PAY13_UNMAPPED_DISPUTE"


@pytest.mark.parametrize(
    "fact_type",
    [
        "accidental_duplicate_provider_payment",
        "orphan_provider_payment",
        "orphan_settlement",
        "unknown_refund",
        "wrong_customer_mapping",
    ],
)
def test_financial_exceptions_never_auto_mutate_money(fact_type):
    plan = plan_financial_fact(
        fact=_fact(fact_type),
        payment=None,
        dispute=None,
    )
    assert isinstance(plan, FinancialExceptionPlan)
    assert plan.quarantine is True
    assert plan.manual_review_required is True
    assert plan.automatic_financial_mutation_allowed is False


def test_won_dispute_reverses_liability_and_releases_hold():
    dispute = DisputeSnapshot(
        status="under_review",
        dispute_type="chargeback",
        amount_minor=11800,
        currency_code="INR",
        financial_hold_active=True,
    )
    plan = plan_financial_fact(
        fact=_fact("dispute_won", decision_ref="decision-win-1"),
        payment=_payment(),
        dispute=dispute,
    )
    assert plan.target_status == "won"
    assert plan.freeze_financial_actions is False
    assert plan.financial_entry_type == "liability_reversed"


def test_lost_dispute_recognizes_loss_without_rewriting_capture():
    dispute = DisputeSnapshot(
        status="under_review",
        dispute_type="chargeback",
        amount_minor=11800,
        currency_code="INR",
        financial_hold_active=True,
    )
    result = FinancialExceptionPlannerService().plan(
        fact=_fact("dispute_lost", decision_ref="decision-loss-1"),
        payment=_payment(),
        dispute=dispute,
    )
    assert result.plan.target_status == "lost"
    assert result.plan.financial_entry_type == "loss_recognized"
    assert result.payment_status_rewrite is None


def test_chargeback_reversal_appends_loss_reversal_without_rewriting_loss_history():
    dispute = DisputeSnapshot(
        status="lost",
        dispute_type="chargeback",
        amount_minor=11800,
        currency_code="INR",
        financial_hold_active=False,
    )
    result = FinancialExceptionPlannerService().plan(
        fact=_fact("chargeback_reversed"),
        payment=_payment(),
        dispute=dispute,
    )
    assert result.plan.target_status is None
    assert result.plan.financial_entry_type == "loss_reversed"
    assert result.payment_status_rewrite is None


def test_duplicate_dispute_fact_is_replay_safe():
    dispute = DisputeSnapshot(
        status="opened",
        dispute_type="chargeback",
        amount_minor=11800,
        currency_code="INR",
        financial_hold_active=True,
    )
    plan = plan_financial_fact(
        fact=_fact("chargeback_opened"),
        payment=_payment(),
        dispute=dispute,
    )
    assert plan.action == "dedupe_existing_dispute"
    assert plan.financial_entry_type is None
