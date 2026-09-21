from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.platform_billing.domain.provider_operations import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderOutcomeKind,
)
from app.platform_billing.domain.recurring import (
    DunningCaseSnapshot,
    DunningDecision,
    DunningPolicy,
    RecurringBillingInput,
    RecurringBillingPlan,
    RecurringEvidenceKind,
    evaluate_dunning,
    plan_recurring_billing,
)
from app.platform_billing.policies.policy_loader import (
    get_dunning_policy_config,
    get_runtime_policy,
)
from app.platform_billing.providers.base import PlatformBillingProvider


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class MandateForRecurringCharge:
    status: str
    payment_rail: str
    valid_until: datetime | None


@dataclass(frozen=True)
class RecurringProviderAttemptResult:
    provider_result: ProviderCallResult
    evidence_kind: str
    dunning_decision: DunningDecision


def current_dunning_policy() -> DunningPolicy:
    config = get_dunning_policy_config()
    return DunningPolicy(
        policy_day_seconds=get_runtime_policy().policy_day_seconds,
        policy_code=config.policy_code,
        full_access_grace_days=config.full_access_grace_days,
        limited_write_stage_days=config.limited_write_stage_days,
        read_only_stage_days=config.read_only_stage_days,
        max_attempts=config.max_attempts,
        retry_spacing_hours=config.retry_spacing_hours,
        final_mode=config.final_mode,
        final_action=config.final_action,
        termination_after_suspension_days=config.termination_after_suspension_days,
    )


def ensure_supported_mandate(
    mandate: MandateForRecurringCharge,
    *,
    now: datetime,
) -> None:
    config = get_dunning_policy_config()
    if mandate.payment_rail not in config.supported_mandate_rails:
        raise ValueError(f"Unsupported recurring payment rail: {mandate.payment_rail!r}")
    if mandate.status != "active":
        raise ValueError(f"Mandate is not chargeable in status {mandate.status!r}")
    if mandate.valid_until is not None and mandate.valid_until <= now:
        raise ValueError("Mandate has expired")


def plan_period(
    *,
    now: datetime,
    billing_period_end: datetime,
    invoice_exists_for_next_period: bool,
    invoice_status: str | None,
    mandate: MandateForRecurringCharge | None,
    payment_attempt_count: int,
) -> RecurringBillingPlan:
    policy = current_dunning_policy()
    mandate_status = mandate.status if mandate is not None else None
    mandate_valid_until = mandate.valid_until if mandate is not None else None
    return plan_recurring_billing(
        RecurringBillingInput(
            now=now,
            billing_period_end=billing_period_end,
            invoice_exists_for_next_period=invoice_exists_for_next_period,
            invoice_status=invoice_status,
            mandate_status=mandate_status,
            mandate_valid_until=mandate_valid_until,
            payment_attempt_count=payment_attempt_count,
            max_attempts=policy.max_attempts,
        )
    )


def classify_provider_result(result: ProviderCallResult) -> str:
    if result.outcome is ProviderOutcomeKind.SUCCESS:
        return RecurringEvidenceKind.payment_succeeded.value
    if result.outcome is ProviderOutcomeKind.BUSINESS_FAILURE:
        if result.error_classification == "customer_action_required":
            return RecurringEvidenceKind.customer_action_required.value
        return RecurringEvidenceKind.confirmed_payment_failure.value
    if result.outcome is ProviderOutcomeKind.RETRYABLE_FAILURE:
        return RecurringEvidenceKind.provider_outage.value
    if result.outcome is ProviderOutcomeKind.TIMEOUT:
        return RecurringEvidenceKind.provider_timeout.value
    if result.outcome is ProviderOutcomeKind.UNKNOWN:
        return RecurringEvidenceKind.provider_unknown.value
    raise ValueError(f"Unsupported provider outcome: {result.outcome!r}")


class RecurringPaymentAttemptService:
    """
    Provider-neutral recurring attempt boundary.

    Callers persist/reserve the invoice and payment attempt first, then invoke
    the provider outside the database transaction. Provider result
    classification is deliberately conservative: retryable technical failures,
    timeouts and unknown outcomes do not count as non-payment.
    """

    def __init__(self, provider: PlatformBillingProvider):
        self._provider = provider

    async def execute(
        self,
        *,
        request: ProviderCallRequest,
        now: datetime,
        existing_dunning: DunningCaseSnapshot | None,
        has_valid_paid_period: bool,
    ) -> RecurringProviderAttemptResult:
        try:
            provider_result = await self._provider.execute(request)
        except Exception:
            provider_result = ProviderCallResult(
                outcome=ProviderOutcomeKind.UNKNOWN,
                error_classification="provider_unexpected_exception",
            )

        evidence_kind = classify_provider_result(provider_result)
        decision = evaluate_dunning(
            now=now,
            evidence_kind=evidence_kind,
            existing=existing_dunning,
            policy=current_dunning_policy(),
            has_valid_paid_period=has_valid_paid_period,
        )
        return RecurringProviderAttemptResult(
            provider_result=provider_result,
            evidence_kind=evidence_kind,
            dunning_decision=decision,
        )
