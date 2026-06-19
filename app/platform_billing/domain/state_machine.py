"""
app/platform_billing/domain/state_machine.py
=============================================
Platform Billing state machine validation.

Exhaustive allowed/forbidden transitions for subscription contract status
and subscription change status. Direct arbitrary status assignment is
forbidden (V2 §9, V3.1 §7).
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping


class SubscriptionContractStatus(str, Enum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    pause_scheduled = "pause_scheduled"
    paused = "paused"
    cancel_scheduled = "cancel_scheduled"
    canceled = "canceled"
    expired = "expired"


class SubscriptionChangeStatus(str, Enum):
    requested = "requested"
    validated = "validated"
    provider_pending = "provider_pending"
    scheduled = "scheduled"
    applied = "applied"
    canceled = "canceled"
    failed_retryable = "failed_retryable"
    failed_final = "failed_final"


# ── Subscription contract transitions ────────────────────────────────

# (from_status, to_status) pairs that are allowed
ALLOWED_CONTRACT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    # Provisioning
    (None, "trialing"),
    (None, "active"),
    # Trial lifecycle
    ("trialing", "active"),
    ("trialing", "expired"),
    ("trialing", "canceled"),
    # Active lifecycle
    ("active", "past_due"),
    ("active", "cancel_scheduled"),
    # Cancel scheduled
    ("cancel_scheduled", "active"),          # undo
    ("cancel_scheduled", "canceled"),        # period end
    # Pause
    ("active", "pause_scheduled"),
    ("pause_scheduled", "active"),           # undo
    ("pause_scheduled", "paused"),
    ("paused", "active"),                     # resume
    ("paused", "cancel_scheduled"),
    ("paused", "canceled"),                  # immediate termination
    # Past due recovery
    ("past_due", "active"),
    ("past_due", "cancel_scheduled"),
    ("past_due", "canceled"),
    # Terminal - no outgoing transitions
    ("canceled", None),   # no-op guard
    ("expired", None),    # no-op guard
})


def validate_contract_transition(
    from_status: str | None,
    to_status: str,
    trigger: str | None = None,
) -> bool:
    """
    Returns True if the transition is allowed.

    Raises ValueError for forbidden transitions with a descriptive message.
    """
    if from_status is None and to_status == "trialing":
        return True
    if from_status is None and to_status == "active":
        return True
    if from_status in ("canceled", "expired") and to_status is None:
        return True  # idempotent terminal check

    key = (from_status, to_status)
    if key in ALLOWED_CONTRACT_TRANSITIONS:
        return True

    raise ValueError(
        f"Forbidden subscription contract transition: "
        f"'{from_status}' -> '{to_status}'"
    )


# ── Subscription change transitions ──────────────────────────────────

ALLOWED_CHANGE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("requested", "validated"),
    ("requested", "failed_final"),
    ("validated", "provider_pending"),
    ("validated", "failed_final"),
    ("provider_pending", "scheduled"),
    ("provider_pending", "applied"),
    ("provider_pending", "failed_retryable"),
    ("failed_retryable", "provider_pending"),
    ("scheduled", "applied"),
    ("scheduled", "canceled"),
    ("requested", "canceled"),
    ("validated", "canceled"),
})


def validate_change_transition(
    from_status: str,
    to_status: str,
) -> bool:
    key = (from_status, to_status)
    if key in ALLOWED_CHANGE_TRANSITIONS:
        return True
    raise ValueError(
        f"Forbidden subscription change transition: "
        f"'{from_status}' -> '{to_status}'"
    )