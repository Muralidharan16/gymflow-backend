"""
app/platform_billing/services/shadow_executor.py
=================================================
Shadow execution foundation for Phase 2.

These are callable-only functions — no automatic hooks, no
startup registration, no Celery beat activation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.platform_billing.domain.access_resolver import (
    AccessResolverInput,
    SubscriptionInput,
    SubscriptionPeriod,
    resolve_access,
)
from app.platform_billing.domain.entitlement_resolver import (
    EntitlementResolverInput,
    resolve_entitlements,
)
from app.platform_billing.services.projection_service import (
    refresh_projections,
    ProjectionRefreshResult,
)
from app.platform_billing.services.shadow_comparison_service import (
    ShadowComparisonService,
    ComparisonResult,
)
from app.platform_billing.observability.metrics import get_metrics, METRIC_NAMES

logger = logging.getLogger("doers.platform_billing.shadow_executor")


@dataclass(frozen=True)
class ShadowExecutionSummary:
    organizations_scanned: int
    organizations_succeeded: int
    organizations_failed: int
    mismatches_detected: int
    results: list[ProjectionRefreshResult | ComparisonResult] = field(default_factory=list)


async def refresh_shadow_projection_for_organization(
    db: AsyncSession,
    organization_id: str,
) -> ProjectionRefreshResult:
    """
    Refresh access and entitlement shadow projections for one organization.
    """
    now = datetime.now(timezone.utc)
    # Build minimal access resolver input from available data
    inputs = AccessResolverInput(
        organization_id=organization_id,
        organization_closed=False,
        subscription=None,
        decision_timestamp=now,
        resolution_version=int(now.timestamp()),
    )

    result = await refresh_projections(
        db=db,
        organization_id=organization_id,
        access_inputs=inputs,
        entitlement_inputs=None,
        emit_audit=False,
    )
    return result


async def compare_shadow_decision_for_organization(
    db: AsyncSession,
    organization_id: str,
) -> ComparisonResult:
    """
    Compare legacy and new access decisions for one organization.
    """
    service = ShadowComparisonService(db)
    now = datetime.now(timezone.utc)

    inputs = AccessResolverInput(
        organization_id=organization_id,
        organization_closed=False,
        subscription=None,
        decision_timestamp=now,
        resolution_version=int(now.timestamp()),
    )

    result = await service.compare_organization(
        organization_id=organization_id,
        new_access_input=inputs,
    )

    metrics = get_metrics()
    if not result.is_exact_match and not result.error:
        metrics.increment(METRIC_NAMES["shadow_mismatch_total"])
    metrics.increment(METRIC_NAMES["shadow_resolution_total"])

    return result


async def backfill_shadow_projections(
    db: AsyncSession,
    batch_size: int = 100,
    cursor: str | None = None,
) -> ShadowExecutionSummary:
    """
    Backfill shadow projections for all organizations.

    Paginated, one organization per transaction, retry-safe.
    """
    metrics = get_metrics()
    summary = ShadowExecutionSummary(organizations_scanned=0, organizations_succeeded=0, organizations_failed=0, mismatches_detected=0)

    # Get all organization IDs
    query = select(text("id")).select_from(text("organizations"))
    if cursor:
        query = query.where(text("id > :cursor")).params(cursor=cursor)
    query = query.order_by(text("id")).limit(batch_size)

    result = await db.execute(query)
    org_rows = result.fetchall()

    for (org_id,) in org_rows:
        org_id_str = str(org_id)
        summary.organizations_scanned += 1

        try:
            # Use a new session per organization
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as org_db:
                proj_result = await refresh_shadow_projection_for_organization(org_db, org_id_str)
                if proj_result.was_updated:
                    metrics.increment(METRIC_NAMES["projection_refresh_total"])
                summary.organizations_succeeded += 1
                summary.results.append(proj_result)

        except Exception as exc:
            logger.exception("Shadow backfill failed for org %s", org_id_str)
            summary.organizations_failed += 1
            metrics.increment(METRIC_NAMES["projection_refresh_failed_total"])

    return summary
