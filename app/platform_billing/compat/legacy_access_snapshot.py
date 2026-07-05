"""
app/platform_billing/compat/legacy_access_snapshot.py
======================================================
Anti-corruption adapter for the current legacy platform access system.

Reads TrialSubscription, Organization.tier, Organization.max_branches
and TIER_LIMITS to produce an immutable comparison snapshot.

Never writes or modifies legacy data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class LegacyAccessSnapshot:
    """Immutable snapshot of legacy platform access state."""

    organization_id: str
    has_trial: bool
    trial_status: str | None  # active | soft_locked | hard_locked | converted
    trial_start: datetime | None
    trial_end: datetime | None
    grace_end: datetime | None
    hard_lock_at: datetime | None
    organization_tier: str | None
    max_branches: int | None
    legacy_access_mode: str  # current computed legacy mode
    snapshot_taken_at: datetime


class LegacyTrialAdapter:
    """
    Reads legacy TrialSubscription and Organization data.

    This adapter exists only for shadow comparison and migration backfill.
    It must never be used for production authorization or enforcement.
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_legacy_snapshot(
        self,
        organization_id: str,
    ) -> LegacyAccessSnapshot | None:
        """Build a legacy access snapshot for one organization."""
        now = datetime.now(timezone.utc)

        # Read trial
        trial_result = await self._db.execute(
            text("""
                SELECT
                    status,
                    trial_start,
                    trial_end,
                    grace_end,
                    hard_lock_at
                FROM trial_subscriptions
                WHERE organization_id = :org_id
            """),
            {"org_id": organization_id},
        )
        trial = trial_result.mappings().one_or_none()

        # Read organization for tier/limits
        org_result = await self._db.execute(
            text("""
                SELECT tier, max_branches
                FROM organizations
                WHERE id = :org_id
            """),
            {"org_id": organization_id},
        )
        org = org_result.mappings().one_or_none()
        if org is None:
            return None

        legacy_mode = self._compute_legacy_mode(trial, now)

        return LegacyAccessSnapshot(
            organization_id=organization_id,
            has_trial=trial is not None,
            trial_status=trial["status"] if trial else None,
            trial_start=trial["trial_start"] if trial else None,
            trial_end=trial["trial_end"] if trial else None,
            grace_end=trial["grace_end"] if trial else None,
            hard_lock_at=trial["hard_lock_at"] if trial else None,
            organization_tier=org["tier"],
            max_branches=org["max_branches"],
            legacy_access_mode=legacy_mode,
            snapshot_taken_at=now,
        )

    def _compute_legacy_mode(
        self,
        trial: Any | None,
        now: datetime,
    ) -> str:
        """Replicate the current trial/tier authorization logic."""
        if trial is None:
            return "blocked"

        status = trial["status"] if trial else "active"

        if status == "converted":
            return "full"
        if status == "hard_locked":
            return "blocked"
        if status == "soft_locked":
            return "read_only"
        if status == "active":
            if now < trial["trial_end"]:
                return "full"
            if now < trial["grace_end"]:
                return "full"  # grace period
            if now < trial["hard_lock_at"]:
                return "read_only"
            return "blocked"

        return "blocked"
