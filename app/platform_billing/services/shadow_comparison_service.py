"""
app/platform_billing/services/shadow_comparison_service.py
===========================================================
Shadow comparison between legacy and new Platform Billing access decisions.

Phase 2 only computes and records differences; it never enforces or
repairs. All mismatch observations are safe structured records with no
customer PII or secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.access_resolver import (
    AccessResolverInput,
    AccessResolverResult,
    AccessDecision,
    SubscriptionInput,
    SubscriptionPeriod,
    SecurityBlock,
    AccessOverrideInput,
    resolve_access,
)
from app.platform_billing.compat.legacy_access_snapshot import (
    LegacyAccessSnapshot,
    LegacyTrialAdapter,
)

logger = logging.getLogger("doers.platform_billing.shadow_comparison")


class MismatchCategory(str, Enum):
    ACCESS_MODE_DIFFERENCE = "access_mode_difference"
    ACCESS_REASON_DIFFERENCE = "access_reason_difference"
    BRANCH_LIMIT_DIFFERENCE = "branch_limit_difference"
    MISSING_NEW_SUBSCRIPTION = "missing_new_subscription"
    MISSING_LEGACY_TRIAL = "missing_legacy_trial"
    PROJECTION_STALE = "projection_stale"
    UNSUPPORTED_LEGACY_MAPPING = "unsupported_legacy_mapping"
    UNSUPPORTED_ADDON_COMPOSITION = "unsupported_addon_composition"
    INCONSISTENT_DURABLE_STATE = "inconsistent_durable_state"
    EXACT_MATCH = "exact_match"


@dataclass(frozen=True)
class ComparisonObservation:
    organization_id: str
    category: str
    legacy_mode: str | None
    new_mode: str | None
    legacy_reason: str | None
    new_reason: str | None
    detail_safe: str = ""
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ComparisonResult:
    organization_id: str
    observations: tuple[ComparisonObservation, ...]
    is_exact_match: bool
    error: str | None = None


class ShadowComparisonService:
    """
    Compares legacy platform access against new resolver output.

    Phase 2: comparison only, no enforcement.
    """

    def __init__(self, db: AsyncSession):
        self._db = db
        self._legacy_adapter = LegacyTrialAdapter(db)

    async def compare_organization(
        self,
        organization_id: str,
        new_access_input: AccessResolverInput,
        new_access_result: AccessResolverResult | None = None,
    ) -> ComparisonResult:
        """
        Compare legacy and new access decisions for one organization.

        Produces structured observations without side effects.
        """
        observations: list[ComparisonObservation] = []

        try:
            # 1. Get legacy snapshot
            legacy = await self._legacy_adapter.get_legacy_snapshot(organization_id)

            if legacy is None:
                observations.append(ComparisonObservation(
                    organization_id=organization_id,
                    category=MismatchCategory.INCONSISTENT_DURABLE_STATE,
                    legacy_mode=None,
                    new_mode=None,
                    legacy_reason=None,
                    new_reason=None,
                    detail_safe="Legacy snapshot unavailable — organization may not exist.",
                ))
                return ComparisonResult(
                    organization_id=organization_id,
                    observations=tuple(observations),
                    is_exact_match=False,
                )

            # 2. Get new access decision
            if new_access_result is None:
                new_access_result = resolve_access(new_access_input)

            new_decision = new_access_result.decision

            # 3. Compare modes
            if legacy.legacy_access_mode != new_decision.mode:
                observations.append(ComparisonObservation(
                    organization_id=organization_id,
                    category=MismatchCategory.ACCESS_MODE_DIFFERENCE,
                    legacy_mode=legacy.legacy_access_mode,
                    new_mode=new_decision.mode,
                    legacy_reason=f"trial_status={legacy.trial_status}",
                    new_reason=new_decision.reason_code,
                    detail_safe=(
                        f"Legacy says '{legacy.legacy_access_mode}' "
                        f"but new resolver says '{new_decision.mode}'"
                    ),
                ))

            # 4. Check for missing new subscription when legacy has trial
            if legacy.has_trial and new_access_input.subscription is None:
                observations.append(ComparisonObservation(
                    organization_id=organization_id,
                    category=MismatchCategory.MISSING_NEW_SUBSCRIPTION,
                    legacy_mode=legacy.legacy_access_mode,
                    new_mode=None,
                    legacy_reason=f"trial_status={legacy.trial_status}",
                    new_reason=None,
                    detail_safe="Legacy has trial but no new Platform Billing subscription exists.",
                ))

            # 5. Check for missing legacy trial when new subscription exists
            if not legacy.has_trial and new_access_input.subscription is not None:
                observations.append(ComparisonObservation(
                    organization_id=organization_id,
                    category=MismatchCategory.MISSING_LEGACY_TRIAL,
                    legacy_mode=legacy.legacy_access_mode,
                    new_mode=new_decision.mode,
                    legacy_reason="No trial found",
                    new_reason=new_decision.reason_code,
                    detail_safe="New subscription exists but legacy has no trial record.",
                ))

            # 6. Record mismatch if any
            if not observations:
                observations.append(ComparisonObservation(
                    organization_id=organization_id,
                    category=MismatchCategory.EXACT_MATCH,
                    legacy_mode=legacy.legacy_access_mode,
                    new_mode=new_decision.mode,
                    legacy_reason=f"trial_status={legacy.trial_status}",
                    new_reason=new_decision.reason_code,
                    detail_safe="Legacy and new decisions match.",
                ))

            is_exact = len(observations) == 1 and observations[0].category == MismatchCategory.EXACT_MATCH

            return ComparisonResult(
                organization_id=organization_id,
                observations=tuple(observations),
                is_exact_match=is_exact,
            )

        except Exception as exc:
            logger.exception("Shadow comparison failed for org %s", organization_id)
            return ComparisonResult(
                organization_id=organization_id,
                observations=(),
                is_exact_match=False,
                error=str(exc),
            )
