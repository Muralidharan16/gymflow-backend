"""
app/platform_billing/domain/freshness.py
=========================================
Projection freshness classification and safe-read fallback computation.

V3.1 §8.4 defines the exact fallback mapping and freshness rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.platform_billing.policies.policy_loader import get_runtime_policy


class ProjectionFreshness(Enum):
    FRESH = "fresh"
    STALE_BEHIND = "stale_behind"
    INVALID_AHEAD = "invalid_ahead"
    MISSING = "missing"


@dataclass(frozen=True)
class FreshnessClassification:
    freshness: ProjectionFreshness
    may_read: bool
    synchronous_recomputation_eligible: bool
    safe_fallback_eligible: bool
    privileged_write_allowed: bool
    message: str


def classify_projection_freshness(
    source_subscription_version: int | None,
    projection_source_subscription_version: int | None,
) -> FreshnessClassification:
    """
    Classify projection freshness by comparing source aggregate versions.

    Rules (V3.1 §8.4):
        - equal → fresh
        - projection behind source → stale
        - projection ahead of source → integrity error
        - missing projection → missing
    """
    if projection_source_subscription_version is None:
        return FreshnessClassification(
            freshness=ProjectionFreshness.MISSING,
            may_read=False,
            synchronous_recomputation_eligible=True,
            safe_fallback_eligible=True,
            privileged_write_allowed=False,
            message="Projection has no source version reference.",
        )

    if source_subscription_version is None:
        # No subscription exists yet — projection is fresh (no source to compare)
        return FreshnessClassification(
            freshness=ProjectionFreshness.FRESH,
            may_read=True,
            synchronous_recomputation_eligible=False,
            safe_fallback_eligible=False,
            privileged_write_allowed=True,
            message="No subscription exists; projection is current.",
        )

    if projection_source_subscription_version == source_subscription_version:
        return FreshnessClassification(
            freshness=ProjectionFreshness.FRESH,
            may_read=True,
            synchronous_recomputation_eligible=False,
            safe_fallback_eligible=False,
            privileged_write_allowed=True,
            message="Projection is fresh.",
        )

    if projection_source_subscription_version < source_subscription_version:
        return FreshnessClassification(
            freshness=ProjectionFreshness.STALE_BEHIND,
            may_read=False,
            synchronous_recomputation_eligible=True,
            safe_fallback_eligible=True,
            privileged_write_allowed=False,
            message=f"Projection at version {projection_source_subscription_version} "
                    f"behind source at {source_subscription_version}.",
        )

    # projection ahead of source — integrity error
    return FreshnessClassification(
        freshness=ProjectionFreshness.INVALID_AHEAD,
        may_read=False,
        synchronous_recomputation_eligible=False,
        safe_fallback_eligible=False,
        privileged_write_allowed=False,
        message=f"Critical integrity error: projection version "
                f"{projection_source_subscription_version} ahead of source "
                f"{source_subscription_version}.",
    )


def resolve_safe_fallback(
    last_mode: str | None,
    has_confirmed_security_block: bool = False,
) -> tuple[str, str]:
    """
    Compute safe-read fallback from the last resolved access mode.

    V3.1 §8.4 exact mapping:
        - A freshly confirmed security block returns (blocked, reason).
        - Otherwise normalize:
            full          → read_only
            limited_write → read_only
            read_only     → read_only
            billing_only  → billing_only
            blocked without freshly confirmed durable block → billing_only
            no last decision → read_only

    Returns (mode, reason) tuple.
    """
    if has_confirmed_security_block:
        return ("blocked", "SECURITY_SUSPENSION")

    mapping = {
        "full": "read_only",
        "limited_write": "read_only",
        "read_only": "read_only",
        "billing_only": "billing_only",
        "blocked": "billing_only",
    }

    fallback = mapping.get(last_mode, "read_only")
    return (fallback, "STALE_PROJECTION_FALLBACK")


def is_operation_safe_for_fallback(
    operation_class: str,
    capability_key: str,
) -> bool:
    """
    Determine if a capability/operation can use safe-read fallback.

    Operations ineligible for fallback (V3.1 §21.6):
        - exports
        - financial actions
        - admin/internal operations
        - capacity changes
        - destructive actions
        - privileged writes
        - security-sensitive reads
    """
    if operation_class in ("financial", "destructive", "increase_capacity"):
        return False

    for prefix in (
        "internal.", "data.export", "security.", "admin.",
    ):
        if capability_key.startswith(prefix):
            return False

    return True