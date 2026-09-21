from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.platform_billing.domain.access_resolver import (
    AccessResolverInput,
    SubscriptionInput,
    SubscriptionPeriod,
    resolve_access,
)
from app.platform_billing.domain.provider_operations import (
    ProviderCallRequest,
    ProviderOutcomeKind,
)
from app.platform_billing.domain.recurring import (
    DunningCaseSnapshot,
    DunningPolicy,
    RecurringEvidenceKind,
    evaluate_dunning,
    replacement_mandate_actions,
    validate_mandate_transition,
)
from app.platform_billing.providers.fake import DeterministicFakeProvider
from app.platform_billing.services.recurring_billing import (
    MandateForRecurringCharge,
    RecurringPaymentAttemptService,
    current_dunning_policy,
    ensure_supported_mandate,
    plan_period,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _policy() -> DunningPolicy:
    return current_dunning_policy()


def _existing(first_failure: datetime = T0, attempts: int = 1) -> DunningCaseSnapshot:
    return DunningCaseSnapshot(
        first_confirmed_failure_at=first_failure,
        confirmed_attempt_count=attempts,
    )


def _request() -> ProviderCallRequest:
    return ProviderCallRequest(
        operation_id=uuid.UUID("99000000-0000-0000-0000-000000000001"),
        organization_id=uuid.UUID("99000000-0000-0000-0000-000000000002"),
        provider_code="fake",
        operation_type="recurring_payment",
        amount_minor=11800,
        currency_code="INR",
        plan_version_id=None,
        price_id=None,
        provider_customer_ref="fake_customer",
        provider_payment_method_ref="fake_method",
        metadata={"rail": "upi_autopay"},
    )


def test_mandate_lifecycle_and_terminal_states():
    assert validate_mandate_transition("pending", "authorized")
    assert validate_mandate_transition("authorized", "active")
    assert validate_mandate_transition("active", "paused")
    assert validate_mandate_transition("paused", "active")
    assert validate_mandate_transition("active", "revoked")
    with pytest.raises(ValueError, match="Terminal mandate"):
        validate_mandate_transition("revoked", "active")
    with pytest.raises(ValueError, match="Forbidden"):
        validate_mandate_transition("pending", "active")


def test_supported_payment_rails_and_expiry():
    for rail in ("upi_autopay", "e_mandate", "card_recurring"):
        ensure_supported_mandate(
            MandateForRecurringCharge(
                status="active",
                payment_rail=rail,
                valid_until=T0 + timedelta(days=30),
            ),
            now=T0,
        )
    with pytest.raises(ValueError, match="expired"):
        ensure_supported_mandate(
            MandateForRecurringCharge(
                status="active",
                payment_rail="upi_autopay",
                valid_until=T0,
            ),
            now=T0,
        )


def test_period_planner_generates_invoice_then_attempts_payment():
    mandate = MandateForRecurringCharge(
        status="active",
        payment_rail="upi_autopay",
        valid_until=T0 + timedelta(days=30),
    )
    not_due = plan_period(
        now=T0,
        billing_period_end=T0 + timedelta(days=1),
        invoice_exists_for_next_period=False,
        invoice_status=None,
        mandate=mandate,
        payment_attempt_count=0,
    )
    assert not_due.actions == ()

    invoice = plan_period(
        now=T0,
        billing_period_end=T0,
        invoice_exists_for_next_period=False,
        invoice_status=None,
        mandate=mandate,
        payment_attempt_count=0,
    )
    assert invoice.actions == ("generate_invoice",)

    payment = plan_period(
        now=T0,
        billing_period_end=T0,
        invoice_exists_for_next_period=True,
        invoice_status="issued",
        mandate=mandate,
        payment_attempt_count=0,
    )
    assert payment.actions == ("create_payment_attempt",)


def test_revoked_or_expired_mandate_routes_to_recovery_not_blind_charge():
    for status in ("revoked", "expired", "failed", "paused"):
        plan = plan_period(
            now=T0,
            billing_period_end=T0,
            invoice_exists_for_next_period=True,
            invoice_status="issued",
            mandate=MandateForRecurringCharge(status=status, payment_rail="upi_autopay", valid_until=None),
            payment_attempt_count=0,
        )
        assert plan.actions == ("record_mandate_unavailable", "notify_customer")


def test_replacement_mandate_binding_is_atomic_ordered_intent():
    assert replacement_mandate_actions(
        current_status="active",
        replacement_status="active",
    ) == ("bind_replacement", "revoke_previous_after_binding")
    assert replacement_mandate_actions(
        current_status="revoked",
        replacement_status="authorized",
    ) == ("bind_replacement",)


def test_time_advanced_dunning_progression_uses_exact_elapsed_seconds():
    first = evaluate_dunning(
        now=T0,
        evidence_kind=RecurringEvidenceKind.confirmed_payment_failure.value,
        existing=None,
        policy=_policy(),
        has_valid_paid_period=True,
    )
    assert first.stage == "full_grace"
    assert first.access_mode == "full"
    assert first.subscription_status == "past_due"
    assert first.full_grace_ends_at == T0 + timedelta(days=3)
    assert first.limited_write_ends_at == T0 + timedelta(days=7)
    assert first.read_only_ends_at == T0 + timedelta(days=14)
    assert first.next_retry_at == T0 + timedelta(hours=24)

    case = _existing()
    at_day_3 = evaluate_dunning(
        now=T0 + timedelta(days=3),
        evidence_kind=RecurringEvidenceKind.provider_outage.value,
        existing=case,
        policy=_policy(),
        has_valid_paid_period=True,
    )
    assert at_day_3.stage == "limited_write"
    assert at_day_3.access_mode == "limited_write"
    assert at_day_3.counts_toward_dunning is False

    at_day_7 = evaluate_dunning(
        now=T0 + timedelta(days=7),
        evidence_kind=RecurringEvidenceKind.provider_timeout.value,
        existing=case,
        policy=_policy(),
        has_valid_paid_period=True,
    )
    assert at_day_7.stage == "read_only"
    assert at_day_7.access_mode == "read_only"

    at_day_14 = evaluate_dunning(
        now=T0 + timedelta(days=14),
        evidence_kind=RecurringEvidenceKind.provider_unknown.value,
        existing=case,
        policy=_policy(),
        has_valid_paid_period=False,
    )
    assert at_day_14.stage == "billing_only"
    assert at_day_14.access_mode == "billing_only"
    assert at_day_14.subscription_status == "paused"
    assert at_day_14.termination_at == T0 + timedelta(days=44)
    assert at_day_14.terminal is False
    assert "subscription_suspended" in at_day_14.notification_types

    at_day_44 = evaluate_dunning(
        now=T0 + timedelta(days=44),
        evidence_kind=RecurringEvidenceKind.provider_unknown.value,
        existing=case,
        policy=_policy(),
        has_valid_paid_period=False,
    )
    assert at_day_44.stage == "billing_only"
    assert at_day_44.access_mode == "billing_only"
    assert at_day_44.subscription_status == "canceled"
    assert at_day_44.terminal is True
    assert "subscription_terminated" in at_day_44.notification_types


def test_provider_outage_alone_never_disables_valid_paying_customer():
    for kind in (
        RecurringEvidenceKind.provider_outage.value,
        RecurringEvidenceKind.provider_timeout.value,
        RecurringEvidenceKind.provider_unknown.value,
    ):
        decision = evaluate_dunning(
            now=T0,
            evidence_kind=kind,
            existing=None,
            policy=_policy(),
            has_valid_paid_period=True,
        )
        assert decision.stage == "healthy"
        assert decision.access_mode == "full"
        assert decision.subscription_status == "active"
        assert decision.counts_toward_dunning is False


def test_late_payment_recovers_access_immediately():
    decision = evaluate_dunning(
        now=T0 + timedelta(days=10),
        evidence_kind=RecurringEvidenceKind.payment_succeeded.value,
        existing=_existing(),
        policy=_policy(),
        has_valid_paid_period=False,
    )
    assert decision.stage == "recovered"
    assert decision.access_mode == "full"
    assert decision.subscription_status == "active"
    assert decision.notification_types == ("payment_recovered",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "expected_evidence", "counts"),
    [
        (ProviderOutcomeKind.SUCCESS, "payment_succeeded", False),
        (ProviderOutcomeKind.BUSINESS_FAILURE, "confirmed_payment_failure", True),
        (ProviderOutcomeKind.RETRYABLE_FAILURE, "provider_outage", False),
        (ProviderOutcomeKind.TIMEOUT, "provider_timeout", False),
        (ProviderOutcomeKind.UNKNOWN, "provider_unknown", False),
    ],
)
async def test_provider_failure_injection(provider_outcome, expected_evidence, counts):
    provider = DeterministicFakeProvider(outcome=provider_outcome)
    result = await RecurringPaymentAttemptService(provider).execute(
        request=_request(),
        now=T0,
        existing_dunning=None,
        has_valid_paid_period=True,
    )
    assert result.evidence_kind == expected_evidence
    assert result.dunning_decision.counts_toward_dunning is counts
    if provider_outcome in {
        ProviderOutcomeKind.RETRYABLE_FAILURE,
        ProviderOutcomeKind.TIMEOUT,
        ProviderOutcomeKind.UNKNOWN,
    }:
        assert result.dunning_decision.access_mode == "full"
        assert result.dunning_decision.subscription_status == "active"


def test_access_resolver_honors_all_pay12_dunning_stages():
    paid_period = SubscriptionPeriod(
        period_type="paid",
        starts_at=T0 - timedelta(days=20),
        ends_at=T0 + timedelta(days=10),
        status="open",
    )
    subscription = SubscriptionInput(
        id="sub",
        version=10,
        status="past_due",
        started_at=T0 - timedelta(days=100),
        current_period_start=paid_period.starts_at,
        current_period_end=paid_period.ends_at,
        cancel_at_period_end=False,
        periods=(paid_period,),
    )

    cases = [
        (dict(within_dunning_full_grace=True), "full"),
        (dict(within_dunning_limited_write=True), "limited_write"),
        (dict(within_read_only_window=True, read_only_reason="PAYMENT_OVERDUE"), "read_only"),
        ({}, "billing_only"),
    ]
    for flags, expected in cases:
        result = resolve_access(
            AccessResolverInput(
                organization_id="org",
                organization_closed=False,
                subscription=subscription,
                has_valid_paid=True,
                decision_timestamp=T0,
                resolution_version=12,
                **flags,
            )
        )
        assert result.decision.mode == expected
        assert result.decision.reason_code in {"PAYMENT_GRACE", "PAYMENT_OVERDUE"}
