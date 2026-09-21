from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable


class MandateStatus(str, Enum):
    pending = "pending"
    authorized = "authorized"
    active = "active"
    paused = "paused"
    revoked = "revoked"
    expired = "expired"
    failed = "failed"


MANDATE_TRANSITIONS = frozenset(
    {
        ("pending", "authorized"),
        ("pending", "failed"),
        ("pending", "revoked"),
        ("authorized", "active"),
        ("authorized", "paused"),
        ("authorized", "revoked"),
        ("authorized", "expired"),
        ("authorized", "failed"),
        ("active", "paused"),
        ("active", "revoked"),
        ("active", "expired"),
        ("active", "failed"),
        ("paused", "active"),
        ("paused", "revoked"),
        ("paused", "expired"),
        ("paused", "failed"),
    }
)


def validate_mandate_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current in {"revoked", "expired", "failed"}:
        raise ValueError(f"Terminal mandate cannot transition: {current!r} -> {target!r}")
    if (current, target) not in MANDATE_TRANSITIONS:
        raise ValueError(f"Forbidden mandate transition: {current!r} -> {target!r}")
    return True


class RecurringEvidenceKind(str, Enum):
    payment_succeeded = "payment_succeeded"
    confirmed_payment_failure = "confirmed_payment_failure"
    mandate_unavailable = "mandate_unavailable"
    customer_action_required = "customer_action_required"
    provider_outage = "provider_outage"
    provider_timeout = "provider_timeout"
    provider_unknown = "provider_unknown"


NON_DUNNING_EVIDENCE = frozenset(
    {
        RecurringEvidenceKind.provider_outage.value,
        RecurringEvidenceKind.provider_timeout.value,
        RecurringEvidenceKind.provider_unknown.value,
    }
)

CONFIRMED_DUNNING_EVIDENCE = frozenset(
    {
        RecurringEvidenceKind.confirmed_payment_failure.value,
        RecurringEvidenceKind.mandate_unavailable.value,
        RecurringEvidenceKind.customer_action_required.value,
    }
)


class DunningStage(str, Enum):
    healthy = "healthy"
    full_grace = "full_grace"
    limited_write = "limited_write"
    read_only = "read_only"
    billing_only = "billing_only"
    recovered = "recovered"


@dataclass(frozen=True)
class DunningPolicy:
    policy_day_seconds: int
    policy_code: str = "DUNNING-IN-V1"
    full_access_grace_days: int = 3
    limited_write_stage_days: int = 4
    read_only_stage_days: int = 7
    max_attempts: int = 4
    retry_spacing_hours: tuple[int, ...] = (0, 24, 72, 120)
    final_mode: str = "billing_only"
    final_action: str = "suspend_then_terminate"
    termination_after_suspension_days: int | None = 30

    def __post_init__(self) -> None:
        if self.policy_day_seconds != 86_400:
            raise ValueError("PAY-12 policy day must be exactly 86400 elapsed seconds")
        if min(
            self.full_access_grace_days,
            self.limited_write_stage_days,
            self.read_only_stage_days,
        ) < 0:
            raise ValueError("Dunning stage durations cannot be negative")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if len(self.retry_spacing_hours) < self.max_attempts:
            raise ValueError("retry_spacing_hours must cover every allowed attempt")
        if tuple(sorted(self.retry_spacing_hours)) != self.retry_spacing_hours:
            raise ValueError("retry spacing must be non-decreasing")
        if self.final_mode != "billing_only":
            raise ValueError("PAY-12 final dunning mode must be billing_only")
        if self.final_action not in {"suspend", "suspend_then_terminate"}:
            raise ValueError("PAY-12 final action must be policy-controlled suspension")
        if self.final_action == "suspend_then_terminate":
            if (
                self.termination_after_suspension_days is None
                or self.termination_after_suspension_days <= 0
            ):
                raise ValueError("PAY-12 termination window must be positive")
        elif self.termination_after_suspension_days is not None:
            raise ValueError(
                "PAY-12 suspension-only policy cannot define termination window"
            )

    def _days(self, days: int) -> timedelta:
        return timedelta(seconds=days * self.policy_day_seconds)

    def boundaries(self, first_confirmed_failure_at: datetime) -> tuple[datetime, datetime, datetime]:
        full_grace_ends_at = first_confirmed_failure_at + self._days(self.full_access_grace_days)
        limited_write_ends_at = full_grace_ends_at + self._days(self.limited_write_stage_days)
        read_only_ends_at = limited_write_ends_at + self._days(self.read_only_stage_days)
        return full_grace_ends_at, limited_write_ends_at, read_only_ends_at

    def retry_at(self, first_confirmed_failure_at: datetime, attempt_number: int) -> datetime | None:
        if attempt_number >= self.max_attempts:
            return None
        hours = self.retry_spacing_hours[attempt_number]
        return first_confirmed_failure_at + timedelta(hours=hours)


@dataclass(frozen=True)
class DunningCaseSnapshot:
    first_confirmed_failure_at: datetime | None = None
    confirmed_attempt_count: int = 0
    recovered_at: datetime | None = None
    terminated_at: datetime | None = None


@dataclass(frozen=True)
class DunningDecision:
    stage: str
    access_mode: str
    subscription_status: str
    counts_toward_dunning: bool
    full_grace_ends_at: datetime | None
    limited_write_ends_at: datetime | None
    read_only_ends_at: datetime | None
    next_retry_at: datetime | None
    next_transition_at: datetime | None
    termination_at: datetime | None = None
    notification_types: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str = ""
    terminal: bool = False


def _stage_for_time(
    *,
    now: datetime,
    first_failure: datetime,
    policy: DunningPolicy,
) -> tuple[str, str, datetime | None]:
    full_end, limited_end, read_end = policy.boundaries(first_failure)
    if now < full_end:
        return DunningStage.full_grace.value, "full", full_end
    if now < limited_end:
        return DunningStage.limited_write.value, "limited_write", limited_end
    if now < read_end:
        return DunningStage.read_only.value, "read_only", read_end
    return DunningStage.billing_only.value, policy.final_mode, None


def evaluate_dunning(
    *,
    now: datetime,
    evidence_kind: str,
    existing: DunningCaseSnapshot | None,
    policy: DunningPolicy,
    has_valid_paid_period: bool,
) -> DunningDecision:
    """
    Pure PAY-12 dunning evaluation.

    A provider outage/timeout/unknown result is explicitly non-customer evidence.
    It cannot create a first confirmed failure or reduce access. If the customer
    still has a valid paid period, access remains full.
    """
    if evidence_kind == RecurringEvidenceKind.payment_succeeded.value:
        return DunningDecision(
            stage=DunningStage.recovered.value,
            access_mode="full",
            subscription_status="active",
            counts_toward_dunning=False,
            full_grace_ends_at=None,
            limited_write_ends_at=None,
            read_only_ends_at=None,
            next_retry_at=None,
            next_transition_at=None,
            termination_at=None,
            notification_types=("payment_recovered",),
            reason_code="PAYMENT_RECOVERED",
            terminal=True,
        )

    if evidence_kind in NON_DUNNING_EVIDENCE:
        existing_stage = (
            DunningStage.healthy.value
            if existing is None
            else _stage_for_existing(now, existing, policy)[0]
        )
        termination_at = _termination_at(existing, policy)
        existing_terminal = bool(
            termination_at is not None and now >= termination_at
        )
        if existing is None or existing.recovered_at is not None:
            subscription_status = "active"
        elif existing_terminal:
            subscription_status = "canceled"
        elif existing_stage == DunningStage.billing_only.value:
            subscription_status = "paused"
        else:
            subscription_status = "past_due"

        return DunningDecision(
            stage=existing_stage,
            access_mode="full" if has_valid_paid_period and existing is None else _access_for_existing(now, existing, policy, has_valid_paid_period),
            subscription_status=subscription_status,
            counts_toward_dunning=False,
            full_grace_ends_at=None if existing is None or existing.first_confirmed_failure_at is None else policy.boundaries(existing.first_confirmed_failure_at)[0],
            limited_write_ends_at=None if existing is None or existing.first_confirmed_failure_at is None else policy.boundaries(existing.first_confirmed_failure_at)[1],
            read_only_ends_at=None if existing is None or existing.first_confirmed_failure_at is None else policy.boundaries(existing.first_confirmed_failure_at)[2],
            next_retry_at=None,
            next_transition_at=(
                None
                if existing is None
                else (
                    termination_at
                    if existing_stage == DunningStage.billing_only.value and not existing_terminal
                    else _stage_for_existing(now, existing, policy)[2]
                )
            ),
            termination_at=termination_at,
            notification_types=(
                ("subscription_terminated",)
                if existing_terminal
                else (
                    ("subscription_suspended",)
                    if existing_stage == DunningStage.billing_only.value
                    else ()
                )
            ),
            reason_code="PROVIDER_OUTAGE_HOLD",
            terminal=existing_terminal,
        )

    if evidence_kind not in CONFIRMED_DUNNING_EVIDENCE:
        raise ValueError(f"Unsupported recurring evidence kind: {evidence_kind!r}")

    first_failure = (
        existing.first_confirmed_failure_at
        if existing and existing.first_confirmed_failure_at is not None
        else now
    )
    prior_attempts = existing.confirmed_attempt_count if existing else 0
    attempt_count = prior_attempts + 1
    stage, access_mode, next_transition = _stage_for_time(
        now=now,
        first_failure=first_failure,
        policy=policy,
    )
    full_end, limited_end, read_end = policy.boundaries(first_failure)
    next_retry = policy.retry_at(first_failure, attempt_count)

    notifications: list[str] = []
    if prior_attempts == 0:
        notifications.append("payment_failed")
    if next_retry is not None:
        notifications.append("retry_scheduled")
    if stage == DunningStage.limited_write.value:
        notifications.append("access_limited")
    elif stage == DunningStage.read_only.value:
        notifications.append("access_read_only")
    elif stage == DunningStage.billing_only.value:
        notifications.append("subscription_suspended")

    termination_at = (
        read_end + policy._days(policy.termination_after_suspension_days)
        if policy.final_action == "suspend_then_terminate"
        and policy.termination_after_suspension_days is not None
        else None
    )
    terminal = bool(termination_at is not None and now >= termination_at)
    subscription_status = (
        "canceled"
        if terminal
        else ("paused" if stage == DunningStage.billing_only.value else "past_due")
    )
    if terminal:
        notifications.append("subscription_terminated")

    return DunningDecision(
        stage=stage,
        access_mode=access_mode,
        subscription_status=subscription_status,
        counts_toward_dunning=True,
        full_grace_ends_at=full_end,
        limited_write_ends_at=limited_end,
        read_only_ends_at=read_end,
        next_retry_at=next_retry,
        next_transition_at=(
            termination_at
            if stage == DunningStage.billing_only.value and not terminal
            else next_transition
        ),
        termination_at=termination_at,
        notification_types=tuple(dict.fromkeys(notifications)),
        reason_code="PAYMENT_OVERDUE",
        terminal=terminal,
    )


def _stage_for_existing(
    now: datetime,
    existing: DunningCaseSnapshot,
    policy: DunningPolicy,
) -> tuple[str, str, datetime | None]:
    if existing.recovered_at is not None:
        return DunningStage.recovered.value, "full", None
    if existing.first_confirmed_failure_at is None:
        return DunningStage.healthy.value, "full", None
    return _stage_for_time(
        now=now,
        first_failure=existing.first_confirmed_failure_at,
        policy=policy,
    )


def _termination_at(
    existing: DunningCaseSnapshot | None,
    policy: DunningPolicy,
) -> datetime | None:
    if (
        existing is None
        or existing.first_confirmed_failure_at is None
        or policy.final_action != "suspend_then_terminate"
        or policy.termination_after_suspension_days is None
    ):
        return None
    read_end = policy.boundaries(existing.first_confirmed_failure_at)[2]
    return read_end + policy._days(policy.termination_after_suspension_days)


def _access_for_existing(
    now: datetime,
    existing: DunningCaseSnapshot | None,
    policy: DunningPolicy,
    has_valid_paid_period: bool,
) -> str:
    if existing is None or existing.first_confirmed_failure_at is None:
        return "full" if has_valid_paid_period else "billing_only"
    return _stage_for_existing(now, existing, policy)[1]


@dataclass(frozen=True)
class RecurringBillingInput:
    now: datetime
    billing_period_end: datetime
    invoice_exists_for_next_period: bool
    invoice_status: str | None
    mandate_status: str | None
    mandate_valid_until: datetime | None
    payment_attempt_count: int
    max_attempts: int


@dataclass(frozen=True)
class RecurringBillingPlan:
    actions: tuple[str, ...]
    reason: str


def plan_recurring_billing(inputs: RecurringBillingInput) -> RecurringBillingPlan:
    if inputs.now < inputs.billing_period_end:
        return RecurringBillingPlan((), "period_not_due")

    if not inputs.invoice_exists_for_next_period:
        return RecurringBillingPlan(("generate_invoice",), "billing_period_arrived")

    if inputs.invoice_status == "paid":
        return RecurringBillingPlan(("advance_paid_period",), "invoice_already_paid")

    mandate_usable = inputs.mandate_status == "active"
    if inputs.mandate_valid_until is not None and inputs.mandate_valid_until <= inputs.now:
        mandate_usable = False

    if not mandate_usable:
        return RecurringBillingPlan(("record_mandate_unavailable", "notify_customer"), "mandate_unavailable")

    if inputs.payment_attempt_count >= inputs.max_attempts:
        return RecurringBillingPlan(("evaluate_dunning_stage",), "retry_budget_exhausted")

    return RecurringBillingPlan(("create_payment_attempt",), "recurring_payment_due")


def replacement_mandate_actions(
    *,
    current_status: str,
    replacement_status: str,
) -> tuple[str, ...]:
    if replacement_status not in {"authorized", "active"}:
        raise ValueError("Replacement mandate must be authorized or active")
    if current_status in {"revoked", "expired", "failed"}:
        return ("bind_replacement",)
    return ("bind_replacement", "revoke_previous_after_binding")


def validate_retry_attempt_numbers(attempt_numbers: Iterable[int]) -> None:
    values = tuple(attempt_numbers)
    if not values:
        return
    if values != tuple(range(1, len(values) + 1)):
        raise ValueError("Dunning attempt numbers must be contiguous from 1")
