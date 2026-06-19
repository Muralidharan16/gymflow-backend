"""
app/platform_billing/services/projection_service.py
===================================================
Transactional projection persistence service for Platform Billing.

Writes entitlement and access projections atomically within an
organization-scoped advisory lock transaction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.platform_billing.domain.access_resolver import (
    AccessResolverInput,
    AccessResolverResult,
    resolve_access,
)
from app.platform_billing.domain.entitlement_resolver import (
    EntitlementResolverInput,
    resolve_entitlements,
)
from app.platform_billing.models.projection import (
    PlatformAccessProjection,
    PlatformEntitlementProjection,
)
from app.platform_billing.policies.policy_loader import get_runtime_policy
from app.platform_billing.models.audit import PlatformBillingAuditEvent

logger = logging.getLogger("doers.platform_billing.projection_service")


@dataclass(frozen=True)
class ProjectionRefreshResult:
    organization_id: str
    entitlement_hash: str | None
    access_hash: str | None
    source_version: int | None
    resolution_version: int
    was_updated: bool
    error: str | None = None


async def refresh_projections(
    db: AsyncSession,
    organization_id: str,
    access_inputs: AccessResolverInput,
    entitlement_inputs: EntitlementResolverInput | None = None,
    *,
    emit_audit: bool = True,
    resolution_version: int | None = None,
) -> ProjectionRefreshResult:
    """
    Refresh entitlement and access projections for an organization.

    Acquires an organization-scoped advisory lock, reads current source
    versions, computes pure decisions, and upserts projections atomically.
    """
    policy = get_runtime_policy()
    lock_namespace = f"platform_billing:projection:{organization_id}"
    lock_hash = hashlib.sha256(
        lock_namespace.encode("utf-8")
    ).hexdigest()
    lock_int = int(lock_hash[:16], 16) & 0x7FFFFFFFFFFFFFFF

    # Acquire advisory transaction lock
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock)"),
        {"lock": lock_int},
    )

    now = datetime.now(timezone.utc)
    rv = resolution_version or int(now.timestamp())

    updated = False
    entitlement_hash = None
    access_hash = None

    try:
        # 1. Resolve access
        access_result = resolve_access(access_inputs)
        access_hash = access_result.input_sha256
        dec = access_result.decision

        # Upsert access projection
        stmt = select(PlatformAccessProjection).where(
            PlatformAccessProjection.organization_id == access_inputs.organization_id
        )
        result = await db.execute(stmt)
        existing_access = result.scalar_one_or_none()

        if existing_access:
            # Only update if input hash changed
            if existing_access.input_sha256 != access_hash:
                existing_access.mode = dec.mode
                existing_access.reason_code = dec.reason_code
                existing_access.reason_detail_safe = dec.reason_detail_safe
                existing_access.effective_from = dec.effective_from
                existing_access.next_transition_at = dec.next_transition_at
                existing_access.recovery_actions_json = list(dec.recovery_actions)
                existing_access.source_subscription_version = dec.source_subscription_version
                existing_access.resolution_version = rv
                existing_access.resolved_at = now
                existing_access.input_sha256 = access_hash
                updated = True
        else:
            db.add(PlatformAccessProjection(
                organization_id=access_inputs.organization_id,
                subscription_id=access_inputs.subscription.id if access_inputs.subscription else None,
                mode=dec.mode,
                reason_code=dec.reason_code,
                reason_detail_safe=dec.reason_detail_safe,
                effective_from=dec.effective_from,
                next_transition_at=dec.next_transition_at,
                recovery_actions_json=list(dec.recovery_actions),
                source_subscription_version=dec.source_subscription_version,
                resolution_version=rv,
                resolved_at=now,
                input_sha256=access_hash,
            ))
            updated = True

        # 2. Resolve entitlements if supplied
        if entitlement_inputs is not None:
            ent_result = resolve_entitlements(entitlement_inputs)
            entitlement_hash = ent_result.input_sha256
            source_subscription_version = entitlement_inputs.subscription_version
            desired_by_feature = {
                resolved.feature_key: resolved
                for resolved in ent_result.entitlements
            }
            existing_rows_result = await db.execute(
                select(PlatformEntitlementProjection).where(
                    PlatformEntitlementProjection.organization_id == organization_id
                )
            )
            existing_by_feature = {
                row.feature_key: row
                for row in existing_rows_result.scalars()
            }

            for stale_key in existing_by_feature.keys() - desired_by_feature.keys():
                await db.delete(existing_by_feature[stale_key])
                updated = True

            for resolved in ent_result.entitlements:
                existing_entitlement = existing_by_feature.get(resolved.feature_key)
                row_values = {
                    "value_type": resolved.value_type,
                    "value_boolean": resolved.value_boolean,
                    "value_integer": resolved.value_integer,
                    "value_string": resolved.value_string,
                    "value_json": resolved.value_json,
                    "source_plan_version_id": resolved.source_plan_version_id,
                    "source_override_id": resolved.source_override_id,
                    "effective_from": resolved.effective_from or entitlement_inputs.decision_timestamp,
                    "effective_until": resolved.effective_until,
                    "source_subscription_version": source_subscription_version,
                    "resolution_version": rv,
                    "input_sha256": entitlement_hash,
                }
                if existing_entitlement is not None:
                    if all(getattr(existing_entitlement, key) == value for key, value in row_values.items()):
                        continue
                    for key, value in row_values.items():
                        setattr(existing_entitlement, key, value)
                    existing_entitlement.resolved_at = now
                    updated = True
                    continue

                db.add(PlatformEntitlementProjection(
                    organization_id=organization_id,
                    feature_key=resolved.feature_key,
                    resolved_at=now,
                    **row_values,
                ))
                updated = True

        # 3. Audit event
        if emit_audit and updated:
            db.add(PlatformBillingAuditEvent(
                organization_id=organization_id,
                actor_type="system",
                action="projection.refresh",
                target_type="projection",
                after_hash=access_hash,
                metadata_redacted_json={
                    "access_mode": dec.mode,
                    "access_reason": dec.reason_code,
                    "resolution_version": rv,
                },
                outcome="succeeded",
            ))

        await db.flush()

        return ProjectionRefreshResult(
            organization_id=organization_id,
            entitlement_hash=entitlement_hash,
            access_hash=access_hash,
            source_version=access_inputs.subscription.version if access_inputs.subscription else None,
            resolution_version=rv,
            was_updated=updated,
        )

    except Exception as exc:
        logger.exception("Failed to refresh projections for org %s", organization_id)
        await db.rollback()
        return ProjectionRefreshResult(
            organization_id=organization_id,
            entitlement_hash=None,
            access_hash=None,
            source_version=None,
            resolution_version=0,
            was_updated=False,
            error=str(exc),
        )
