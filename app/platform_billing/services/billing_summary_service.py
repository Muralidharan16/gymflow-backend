from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.api.schemas import (
    PlatformBillingAccessSummary,
    PlatformBillingDecisionAvailability,
    PlatformBillingEntitlementSummary,
    PlatformBillingPeriodSummary,
    PlatformBillingPlanSummary,
    PlatformBillingSummaryResponse,
    PlatformBillingUsageSummary,
)
from app.platform_billing.domain.freshness import classify_projection_freshness
from app.platform_billing.models.projection import (
    PlatformAccessProjection,
    PlatformEntitlementProjection,
    PlatformUsageProjection,
)
from app.platform_billing.repositories.subscriptions import CURRENT_SUBSCRIPTION_STATUSES
from app.platform_billing.models.subscription import PlatformSubscription
from app.platform_billing.services.query_service import PlatformBillingQueryService


class PlatformBillingSummaryService:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._query = PlatformBillingQueryService(db)

    async def get_summary(self, organization_id: uuid.UUID) -> PlatformBillingSummaryResponse:
        now = datetime.now(timezone.utc)
        subscription_detail = await self._query.get_current_subscription(organization_id)
        access_projection = await self._get_access_projection(organization_id)
        source_version = await self._current_source_version(organization_id)
        freshness = classify_projection_freshness(
            source_subscription_version=source_version,
            projection_source_subscription_version=(
                access_projection.source_subscription_version if access_projection else None
            ),
        )

        entitlements = await self._get_entitlements(organization_id)
        usage_rows = await self._get_usage(organization_id)
        entitlement_limits = {
            item.key: item.value
            for item in entitlements
            if item.value_type == "integer" and isinstance(item.value, int)
        }

        plan_summary = PlatformBillingPlanSummary()
        period_summary = PlatformBillingPeriodSummary()
        if subscription_detail is not None:
            sub = subscription_detail.subscription
            plan = await self._query.get_plan_detail(sub.current_plan_version_id, now=now)
            plan_summary = PlatformBillingPlanSummary(
                code=plan.code if plan else None,
                display_name=plan.display_name if plan else None,
                status=plan.status if plan else None,
            )
            period_summary = PlatformBillingPeriodSummary(
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                subscription_status=sub.status,
                cancel_at_period_end=sub.cancel_at_period_end,
            )

        if access_projection is None:
            access = PlatformBillingAccessSummary(
                mode="read_only",
                safe_reason_code="DECISION_UNAVAILABLE",
                recovery_actions=["VIEW_PLAN_BILLING", "CONTACT_SUPPORT"],
                projection_freshness=freshness.freshness.value,
            )
            availability = PlatformBillingDecisionAvailability(
                available=False,
                reason="projection_missing",
            )
        else:
            access = PlatformBillingAccessSummary(
                mode=access_projection.mode,
                safe_reason_code=access_projection.reason_code,
                effective_from=access_projection.effective_from,
                next_transition_at=access_projection.next_transition_at,
                recovery_actions=list(access_projection.recovery_actions_json),
                projection_freshness=freshness.freshness.value,
            )
            availability = PlatformBillingDecisionAvailability(
                available=freshness.freshness.value == "fresh",
                reason=None if freshness.freshness.value == "fresh" else freshness.freshness.value,
            )

        return PlatformBillingSummaryResponse(
            organization_id=str(organization_id),
            access=access,
            plan=plan_summary,
            billing_period=period_summary,
            entitlements=entitlements,
            usage=[
                PlatformBillingUsageSummary(
                    key=row.metric_key,
                    current=row.current_value,
                    limit=(
                        entitlement_limits.get(row.metric_key)
                        if isinstance(entitlement_limits.get(row.metric_key), int)
                        else None
                    ),
                    over_limit=(
                        row.current_value >= entitlement_limits[row.metric_key]
                        if isinstance(entitlement_limits.get(row.metric_key), int)
                        else None
                    ),
                    stale_after=row.stale_after,
                )
                for row in usage_rows
            ],
            decision_availability=availability,
            server_time=now,
        )

    async def _current_source_version(self, organization_id: uuid.UUID) -> int | None:
        result = await self._db.execute(
            select(PlatformSubscription.version)
            .where(
                PlatformSubscription.organization_id == organization_id,
                PlatformSubscription.status.in_(CURRENT_SUBSCRIPTION_STATUSES),
            )
            .order_by(PlatformSubscription.created_at.desc())
        )
        return result.scalars().first()

    async def _get_access_projection(
        self,
        organization_id: uuid.UUID,
    ) -> PlatformAccessProjection | None:
        result = await self._db.execute(
            select(PlatformAccessProjection).where(
                PlatformAccessProjection.organization_id == organization_id
            )
        )
        return result.scalar_one_or_none()

    async def _get_entitlements(
        self,
        organization_id: uuid.UUID,
    ) -> list[PlatformBillingEntitlementSummary]:
        result = await self._db.execute(
            select(PlatformEntitlementProjection)
            .where(PlatformEntitlementProjection.organization_id == organization_id)
            .order_by(PlatformEntitlementProjection.feature_key)
        )
        return [
            PlatformBillingEntitlementSummary(
                key=row.feature_key,
                value_type=row.value_type,
                value=_entitlement_value(row),
            )
            for row in result.scalars().all()
        ]

    async def _get_usage(self, organization_id: uuid.UUID) -> list[PlatformUsageProjection]:
        result = await self._db.execute(
            select(PlatformUsageProjection)
            .where(PlatformUsageProjection.organization_id == organization_id)
            .order_by(PlatformUsageProjection.metric_key)
        )
        return list(result.scalars().all())


def _entitlement_value(row: PlatformEntitlementProjection) -> bool | int | str | dict[str, Any]:
    if row.value_type == "boolean":
        return bool(row.value_boolean)
    if row.value_type == "integer":
        return int(row.value_integer or 0)
    if row.value_type == "string":
        return row.value_string or ""
    if row.value_type == "json":
        return dict(row.value_json or {})
    return ""
