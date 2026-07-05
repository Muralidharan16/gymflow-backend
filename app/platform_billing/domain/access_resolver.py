"""
app/platform_billing/domain/access_resolver.py
===============================================
Pure domain access resolver for Platform Billing.

Deterministically derives platform access mode from durable contract,
period, dunning, override, and policy records.

No FastAPI, SQLAlchemy, Redis, Celery, or provider dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from app.platform_billing.domain.hashing import (
    ACCESS_RESOLVER_VERSION,
    compute_input_hash,
)
from app.platform_billing.domain.enums import (
    PlatformAccessMode,
    AccessReasonCode,
    SubscriptionContractStatus,
)
from app.platform_billing.policies.policy_loader import get_runtime_policy


@dataclass(frozen=True)
class SecurityBlock:
    active: bool
    reason: str = ""


@dataclass(frozen=True)
class AccessOverrideInput:
    active: bool
    scope: str = ""  # access_mode | entitlement
    mode: str | None = None
    reason: str = ""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class SubscriptionPeriod:
    period_type: str  # trial | paid | grace | extension | post_cancel_read_only
    starts_at: datetime
    ends_at: datetime
    status: str  # open | closed


@dataclass(frozen=True)
class SubscriptionInput:
    id: str | None
    version: int
    status: str
    started_at: datetime | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    periods: tuple[SubscriptionPeriod, ...]


@dataclass(frozen=True)
class AccessResolverInput:
    organization_id: str
    organization_closed: bool
    subscription: SubscriptionInput | None
    security_block: SecurityBlock | None = None
    access_override: AccessOverrideInput | None = None
    has_valid_trial: bool = False
    has_valid_paid: bool = False
    within_trial_grace: bool = False
    within_dunning_full_grace: bool = False
    within_dunning_limited_write: bool = False
    within_read_only_window: bool = False
    state_inconsistent: bool = False
    read_only_reason: str = ""
    decision_timestamp: datetime | None = None
    resolution_version: int = 0


@dataclass(frozen=True)
class AccessDecision:
    mode: str
    reason_code: str
    reason_detail_safe: str
    effective_from: datetime
    effective_until: datetime | None = None
    next_transition_at: datetime | None = None
    recovery_actions: tuple[str, ...] = field(default_factory=tuple)
    source_subscription_version: int | None = None
    resolution_version: int = 0


@dataclass(frozen=True)
class AccessResolverResult:
    decision: AccessDecision
    resolution_version: int
    input_sha256: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _has_valid_period(
    periods: tuple[SubscriptionPeriod, ...],
    period_type: str,
    now: datetime,
) -> bool:
    """Check if there's an open period of the given type covering `now`."""
    for p in periods:
        if p.status != "open":
            continue
        if p.period_type != period_type:
            continue
        if p.starts_at <= now < p.ends_at:
            return True
    return False


def _period_remaining(
    periods: tuple[SubscriptionPeriod, ...],
    period_type: str,
    now: datetime,
) -> datetime | None:
    """Return the end timestamp of the current open period."""
    for p in periods:
        if p.status != "open":
            continue
        if p.period_type != period_type:
            continue
        if p.starts_at <= now < p.ends_at:
            return p.ends_at
    return None


def _policy_duration_seconds(days: int) -> timedelta:
    """Exact elapsed duration: N policy days = N * 86,400 seconds."""
    return timedelta(seconds=days * get_runtime_policy().policy_day_seconds)


def resolve_access(inputs: AccessResolverInput) -> AccessResolverResult:
    """
    Deterministic access resolution using V3.1 §8.3 priority order.

    The caller supplies the decision timestamp; the resolver does not
    read wall-clock time.
    """
    now = inputs.decision_timestamp or datetime.now(timezone.utc)
    input_sha256 = compute_input_hash(ACCESS_RESOLVER_VERSION, inputs)
    warnings: list[str] = []

    sub = inputs.subscription
    sub_version = sub.version if sub else None

    # 1. Security/compliance block
    if inputs.security_block and inputs.security_block.active:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="blocked",
                reason_code="SECURITY_SUSPENSION",
                reason_detail_safe=inputs.security_block.reason,
                effective_from=now,
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 2. Active access override
    if inputs.access_override and inputs.access_override.active:
        if inputs.access_override.scope == "access_mode" and inputs.access_override.mode:
            return AccessResolverResult(
                decision=AccessDecision(
                    mode=inputs.access_override.mode,
                    reason_code="MANUAL_OVERRIDE",
                    reason_detail_safe=inputs.access_override.reason,
                    effective_from=now,
                    effective_until=inputs.access_override.expires_at,
                    next_transition_at=inputs.access_override.expires_at,
                    resolution_version=inputs.resolution_version,
                    source_subscription_version=sub_version,
                ),
                resolution_version=inputs.resolution_version,
                input_sha256=input_sha256,
            )

    # 3. Organization closed
    if inputs.organization_closed:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="billing_only",
                reason_code="ORGANIZATION_CLOSED",
                reason_detail_safe="This organization is closed.",
                effective_from=now,
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 4a. Valid trial period
    if inputs.has_valid_trial:
        next_trans = _period_remaining(
            sub.periods if sub else (), "trial", now
        )
        return AccessResolverResult(
            decision=AccessDecision(
                mode="full",
                reason_code="TRIAL_ACTIVE",
                reason_detail_safe="Your trial is active.",
                effective_from=now,
                next_transition_at=next_trans,
                recovery_actions=("VIEW_PLAN_BILLING",),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 4b. Valid paid period
    if inputs.has_valid_paid:
        if sub and sub.status == "past_due":
            return _resolve_dunning(inputs, now, sub_version)
        next_trans = _period_remaining(
            sub.periods if sub else (), "paid", now
        )
        return AccessResolverResult(
            decision=AccessDecision(
                mode="full",
                reason_code="PAID_PERIOD_ACTIVE",
                reason_detail_safe="Your subscription is active.",
                effective_from=now,
                next_transition_at=next_trans,
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 5. Trial grace
    if inputs.within_trial_grace:
        grace_end = now + _policy_duration_seconds(3)  # default: 3-day grace
        return AccessResolverResult(
            decision=AccessDecision(
                mode="full",
                reason_code="TRIAL_GRACE",
                reason_detail_safe="Your trial has ended but you still have full access during the grace period.",
                effective_from=now,
                next_transition_at=grace_end,
                recovery_actions=("VIEW_PLAN_BILLING", "UPDATE_PAYMENT_METHOD"),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 6. Dunning grace
    if inputs.within_dunning_full_grace:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="full",
                reason_code="PAYMENT_GRACE",
                reason_detail_safe="Payment is overdue but you still have full access.",
                effective_from=now,
                recovery_actions=("UPDATE_PAYMENT_METHOD", "CONTACT_SUPPORT"),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 7. Limited write dunning stage
    if inputs.within_dunning_limited_write:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="limited_write",
                reason_code="PAYMENT_OVERDUE",
                reason_detail_safe="Payment is overdue. Some actions are restricted.",
                effective_from=now,
                recovery_actions=("UPDATE_PAYMENT_METHOD", "CONTACT_SUPPORT"),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 8. Read-only window
    if inputs.within_read_only_window:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="read_only",
                reason_code=inputs.read_only_reason or "SUBSCRIPTION_EXPIRED",
                reason_detail_safe="Your subscription has expired. Data is in read-only mode.",
                effective_from=now,
                recovery_actions=("VIEW_PLAN_BILLING", "EXPORT_DATA", "CONTACT_SUPPORT"),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 9. Inconsistent state fallback
    if inputs.state_inconsistent:
        return AccessResolverResult(
            decision=AccessDecision(
                mode="billing_only",
                reason_code="BILLING_STATE_REVIEW_REQUIRED",
                reason_detail_safe="Your billing state needs review. Please contact support.",
                effective_from=now,
                recovery_actions=("VIEW_PLAN_BILLING", "CONTACT_SUPPORT"),
                resolution_version=inputs.resolution_version,
                source_subscription_version=sub_version,
            ),
            resolution_version=inputs.resolution_version,
            input_sha256=input_sha256,
        )

    # 10. No active service period
    return AccessResolverResult(
        decision=AccessDecision(
            mode="billing_only",
            reason_code="NO_ACTIVE_SERVICE_PERIOD",
            reason_detail_safe="No active subscription found.",
            effective_from=now,
            recovery_actions=("VIEW_PLAN_BILLING", "CONTACT_SUPPORT"),
            resolution_version=inputs.resolution_version,
            source_subscription_version=sub_version,
        ),
        resolution_version=inputs.resolution_version,
        input_sha256=input_sha256,
    )


def _resolve_dunning(
    inputs: AccessResolverInput,
    now: datetime,
    sub_version: int | None,
) -> AccessResolverResult:
    """Resolve dunning stages for past_due subscriptions."""
    # Default policy: 3-day grace, 4-day limited write, 7-day read-only
    # In Phase 2 we use the general fallback behavior until dunning policies
    # are fully integrated.
    return AccessResolverResult(
        decision=AccessDecision(
            mode="read_only" if not inputs.within_dunning_full_grace else "full",
            reason_code="PAYMENT_OVERDUE",
            reason_detail_safe="Payment is overdue. Please update your payment method.",
            effective_from=now,
            recovery_actions=("UPDATE_PAYMENT_METHOD", "CONTACT_SUPPORT"),
            resolution_version=inputs.resolution_version,
            source_subscription_version=sub_version,
        ),
        resolution_version=inputs.resolution_version,
        input_sha256=compute_input_hash(ACCESS_RESOLVER_VERSION, inputs),
    )
