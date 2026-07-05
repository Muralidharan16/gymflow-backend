from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.capability_decision import (
    CapabilityDecision,
    CapabilityDecisionInput,
    CapabilityEntitlementValue,
    CapabilityUsageValue,
)
from app.platform_billing.domain.capability_resolver import resolve_capability_decision
from app.platform_billing.domain.freshness import (
    ProjectionFreshness,
    classify_projection_freshness,
    resolve_safe_fallback,
)
from app.platform_billing.models.audit import PlatformBillingAuditEvent
from app.platform_billing.models.projection import (
    PlatformAccessProjection,
    PlatformEntitlementProjection,
    PlatformUsageProjection,
)
from app.platform_billing.models.subscription import PlatformSubscription
from app.platform_billing.observability.metrics import METRIC_NAMES, get_metrics
from app.platform_billing.policies.capability_registry import get_capability_registry
from app.platform_billing.policies.policy_loader import get_runtime_policy
from app.platform_billing.repositories.subscriptions import CURRENT_SUBSCRIPTION_STATUSES


RecomputeCallback = Callable[[AsyncSession, uuid.UUID], Awaitable[bool]]


@dataclass(frozen=True)
class AuthorizationServiceResult:
    decision: CapabilityDecision
    recompute_error: str | None = None


class CapabilityAuthorizationService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        recompute_callback: RecomputeCallback | None = None,
    ):
        self._db = db
        self._recompute_callback = recompute_callback

    async def authorize(
        self,
        *,
        organization_id: uuid.UUID,
        capability_key: str,
        operation_class: str,
        correlation_id: str | None = None,
    ) -> AuthorizationServiceResult:
        now = datetime.now(timezone.utc)
        registry = get_capability_registry()
        capability = registry.get(capability_key)
        source_version = await self._current_source_version(organization_id)
        access_projection = await self._get_access_projection(organization_id)

        freshness = classify_projection_freshness(
            source_subscription_version=source_version,
            projection_source_subscription_version=(
                access_projection.source_subscription_version
                if access_projection is not None
                else None
            ),
        )

        recompute_attempted = False
        recompute_error: str | None = None
        if freshness.synchronous_recomputation_eligible and self._recompute_callback is not None:
            recompute_attempted = True
            try:
                timeout = get_runtime_policy().access_resolution_sync_timeout_ms / 1000
                recomputed = await asyncio.wait_for(
                    self._recompute_callback(self._db, organization_id),
                    timeout=timeout,
                )
                if recomputed:
                    access_projection = await self._get_access_projection(organization_id)
                    source_version = await self._current_source_version(organization_id)
                    freshness = classify_projection_freshness(
                        source_subscription_version=source_version,
                        projection_source_subscription_version=(
                            access_projection.source_subscription_version
                            if access_projection is not None
                            else None
                        ),
                    )
            except Exception as exc:
                recompute_error = exc.__class__.__name__

        fallback_used = False
        access_mode = access_projection.mode if access_projection is not None else None
        if freshness.freshness in {ProjectionFreshness.STALE_BEHIND, ProjectionFreshness.MISSING}:
            if capability is not None and capability.fallback_eligible:
                access_mode, _reason = resolve_safe_fallback(access_mode)
                fallback_used = True
                get_metrics().increment(
                    METRIC_NAMES["capability_fallback_total"],
                    _labels(
                        capability_key=capability_key,
                        operation_class=operation_class,
                        access_mode=access_mode,
                        decision_code="fallback",
                        freshness=freshness.freshness.value,
                        fallback_used=True,
                    ),
                )

        entitlements = await self._load_entitlements(organization_id)
        usage = await self._load_usage(organization_id, capability.usage_metric_key if capability else None)
        decision = resolve_capability_decision(
            CapabilityDecisionInput(
                organization_id=str(organization_id),
                capability_key=capability_key,
                operation_class=operation_class,
                decision_timestamp=now,
                access_mode=access_mode,
                projection_freshness=freshness.freshness.value,
                entitlements=entitlements,
                usage=usage,
                fallback_used=fallback_used,
                recompute_attempted=recompute_attempted,
                source_subscription_version=source_version,
            )
        )

        get_metrics().increment(
            METRIC_NAMES["capability_decision_total"],
            _labels(
                capability_key=capability_key,
                operation_class=operation_class,
                access_mode=access_mode or "none",
                decision_code=decision.decision_code,
                freshness=decision.projection_freshness,
                fallback_used=decision.fallback_used,
            ),
        )
        if not decision.allowed:
            metric_key = (
                "capability_unavailable_total"
                if decision.decision_code in {"ACCESS_DECISION_UNAVAILABLE", "PLATFORM_PROJECTION_INVALID"}
                else "capability_denied_total"
            )
            get_metrics().increment(
                METRIC_NAMES[metric_key],
                _labels(
                    capability_key=capability_key,
                    operation_class=operation_class,
                    access_mode=access_mode or "none",
                    decision_code=decision.decision_code,
                    freshness=decision.projection_freshness,
                    fallback_used=decision.fallback_used,
                ),
            )

        if decision.decision_code == "PLATFORM_PROJECTION_INVALID":
            self._db.add(
                PlatformBillingAuditEvent(
                    organization_id=organization_id,
                    actor_type="system",
                    action="capability.projection_invalid",
                    target_type="capability",
                    metadata_redacted_json={
                        "capability_key": capability_key,
                        "operation_class": operation_class,
                        "correlation_id": correlation_id,
                    },
                    outcome="denied",
                    reason_code=decision.decision_code,
                )
            )

        return AuthorizationServiceResult(decision=decision, recompute_error=recompute_error)

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

    async def _load_entitlements(
        self,
        organization_id: uuid.UUID,
    ) -> tuple[CapabilityEntitlementValue, ...]:
        result = await self._db.execute(
            select(PlatformEntitlementProjection).where(
                PlatformEntitlementProjection.organization_id == organization_id
            )
        )
        values: list[CapabilityEntitlementValue] = []
        for row in result.scalars().all():
            values.append(
                CapabilityEntitlementValue(
                    feature_key=row.feature_key,
                    value_type=row.value_type,
                    value=_row_entitlement_value(row),
                )
            )
        return tuple(values)

    async def _load_usage(
        self,
        organization_id: uuid.UUID,
        metric_key: str | None,
    ) -> tuple[CapabilityUsageValue, ...]:
        if metric_key is None:
            return ()
        result = await self._db.execute(
            select(PlatformUsageProjection).where(
                PlatformUsageProjection.organization_id == organization_id,
                PlatformUsageProjection.metric_key == metric_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return ()
        return (
            CapabilityUsageValue(
                metric_key=row.metric_key,
                current_value=row.current_value,
            ),
        )


def _row_entitlement_value(
    row: PlatformEntitlementProjection,
) -> bool | int | str | dict:
    if row.value_type == "boolean":
        return bool(row.value_boolean)
    if row.value_type == "integer":
        if row.value_integer is None:
            raise ValueError(f"integer entitlement {row.feature_key} is missing value_integer")
        return int(row.value_integer)
    if row.value_type == "string":
        return str(row.value_string or "")
    if row.value_type == "json":
        return dict(row.value_json or {})
    raise ValueError(f"unsupported entitlement value_type {row.value_type!r}")


def _labels(
    *,
    capability_key: str,
    operation_class: str,
    access_mode: str,
    decision_code: str,
    freshness: str,
    fallback_used: bool,
) -> dict[str, str]:
    return {
        "capability_key": capability_key,
        "operation_class": operation_class,
        "access_mode": access_mode,
        "decision_code": decision_code,
        "freshness": freshness,
        "fallback_used": "true" if fallback_used else "false",
    }
