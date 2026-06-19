"""
app/platform_billing/services/usage_service.py
===============================================
Usage measurement and projection service for Platform Billing.

Measures supported usage metrics from authoritative source tables
and writes projection rows. Phase 2: measurement only, no enforcement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.models.projection import PlatformUsageProjection

logger = logging.getLogger("doers.platform_billing.usage_service")

SUPPORTED_METRICS = frozenset({
    "limits.branches.active",
    "limits.members.active",
    "limits.staff.active",
    "limits.membership_plans.active",
})


@dataclass(frozen=True)
class UsageMeasurement:
    metric_key: str
    current_value: int
    measured_at: datetime
    source_high_watermark: str | None
    stale_after: datetime


@dataclass(frozen=True)
class MeasurementResult:
    metric_key: str
    current_value: int | None
    error: str | None = None
    supported: bool = True


class UsageMeasurementService:
    """
    Measures and projects supported usage metrics.

    Phase 2 only; no enforcement or capacity change.
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def measure(self, metric_key: str, organization_id: str) -> MeasurementResult:
        """Measure one supported metric from authoritative source tables."""
        if metric_key not in SUPPORTED_METRICS:
            return MeasurementResult(
                metric_key=metric_key,
                current_value=None,
                error=f"Unsupported metric: {metric_key}",
                supported=False,
            )

        try:
            if metric_key == "limits.branches.active":
                value = await self._count_active_branches(organization_id)
            elif metric_key == "limits.members.active":
                value = await self._count_active_members(organization_id)
            elif metric_key == "limits.staff.active":
                value = await self._count_active_staff(organization_id)
            elif metric_key == "limits.membership_plans.active":
                value = await self._count_active_membership_plans(organization_id)
            else:
                return MeasurementResult(
                    metric_key=metric_key,
                    current_value=None,
                    error=f"Unsupported metric: {metric_key}",
                    supported=False,
                )

            return MeasurementResult(
                metric_key=metric_key,
                current_value=value,
            )

        except Exception as exc:
            logger.exception("Failed to measure %s for org %s", metric_key, organization_id)
            return MeasurementResult(
                metric_key=metric_key,
                current_value=None,
                error=str(exc),
            )

    async def project_usage(
        self,
        organization_id: str,
        measurements: Sequence[MeasurementResult],
    ) -> None:
        """Write usage projection rows for measured metrics."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        stale_after = now + timedelta(hours=25)

        for measurement in measurements:
            if measurement.current_value is None or not measurement.supported:
                continue

            # Upsert usage projection
            stmt = text("""
                INSERT INTO platform_usage_projection
                    (organization_id, metric_key, current_value, measured_at, source_high_watermark, stale_after)
                VALUES
                    (:org_id, :metric_key, :value, :measured_at, :watermark, :stale_after)
                ON CONFLICT (organization_id, metric_key)
                DO UPDATE SET
                    current_value = EXCLUDED.current_value,
                    measured_at = EXCLUDED.measured_at,
                    stale_after = EXCLUDED.stale_after
            """)
            await self._db.execute(
                stmt,
                {
                    "org_id": organization_id,
                    "metric_key": measurement.metric_key,
                    "value": measurement.current_value,
                    "measured_at": now,
                    "watermark": str(now.timestamp()),
                    "stale_after": stale_after,
                },
            )

    async def _count_active_branches(self, organization_id: str) -> int:
        result = await self._db.execute(
            text("""
                SELECT count(*)::bigint
                FROM gyms
                WHERE org_id = :org_id
                  AND is_active IS TRUE
            """),
            {"org_id": organization_id},
        )
        return result.scalar() or 0

    async def _count_active_members(self, organization_id: str) -> int:
        result = await self._db.execute(
            text("""
                SELECT count(*)::bigint
                FROM members
                WHERE org_id = :org_id
                  AND is_active IS TRUE
                  AND status = 'active'
            """),
            {"org_id": organization_id},
        )
        return result.scalar() or 0

    async def _count_active_staff(self, organization_id: str) -> int:
        result = await self._db.execute(
            text("""
                SELECT count(*)::bigint
                FROM organization_users
                WHERE org_id = :org_id
                  AND is_active IS TRUE
                  AND deleted_at IS NULL
            """),
            {"org_id": organization_id},
        )
        return result.scalar() or 0

    async def _count_active_membership_plans(self, organization_id: str) -> int:
        result = await self._db.execute(
            text("""
                SELECT count(*)::bigint
                FROM membership_plans
                WHERE org_id = :org_id
                  AND status = 'active'
            """),
            {"org_id": organization_id},
        )
        return result.scalar() or 0
