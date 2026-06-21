from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.reconciliation import (
    ProviderOperationEvidence,
    ReconciliationClaimLost,
    ReconciliationItemNotFound,
    ReconciliationItemSnapshot,
    ReconciliationRunClaimLost,
    ReconciliationRunConflict,
    ReconciliationRunNotFound,
    ReconciliationRunRequest,
    ReconciliationRunSnapshot,
    compute_run_identity,
)
from app.platform_billing.models.reconciliation import (
    PlatformReconciliationItem,
    PlatformReconciliationRun,
)


class PlatformReconciliationRunRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def reserve(self, request: ReconciliationRunRequest) -> ReconciliationRunSnapshot:
        run_identity = compute_run_identity(request)
        scope_json = dict(request.scope)
        if request.organization_id is not None:
            scope_json = scope_json | {"organization_id": str(request.organization_id)}
        statement = (
            insert(PlatformReconciliationRun)
            .values(
                provider_code=request.provider_code,
                run_identity=run_identity,
                status="running",
                claim_state="idle",
                scope_json=scope_json,
                watermark_json=dict(request.watermark),
            )
            .on_conflict_do_nothing(constraint="uq_platform_reconciliation_runs_identity")
            .returning(PlatformReconciliationRun)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_run_snapshot(row), was_created=True)

        existing = await self.get_by_identity(provider_code=request.provider_code, run_identity=run_identity, for_update=True)
        if existing is None:
            raise ReconciliationRunConflict("Reconciliation run conflicted but no existing run was visible")
        return existing

    async def get_by_identity(
        self,
        *,
        provider_code: str,
        run_identity: str,
        for_update: bool = False,
    ) -> ReconciliationRunSnapshot | None:
        statement = select(PlatformReconciliationRun).where(
            PlatformReconciliationRun.provider_code == provider_code,
            PlatformReconciliationRun.run_identity == run_identity,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _run_snapshot(row) if row is not None else None

    async def get_by_id(self, run_id: uuid.UUID, *, for_update: bool = False) -> ReconciliationRunSnapshot | None:
        statement = select(PlatformReconciliationRun).where(PlatformReconciliationRun.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _run_snapshot(row) if row is not None else None

    async def claim(self, run_id: uuid.UUID, *, now: datetime, expires_at: datetime) -> ReconciliationRunSnapshot:
        statement = (
            update(PlatformReconciliationRun)
            .where(
                PlatformReconciliationRun.id == run_id,
                PlatformReconciliationRun.status == "running",
                (
                    (PlatformReconciliationRun.claim_state == "idle")
                    | (
                        (PlatformReconciliationRun.claim_state == "processing")
                        & (PlatformReconciliationRun.claim_expires_at < now)
                    )
                ),
            )
            .values(
                claim_state="processing",
                attempt_count=PlatformReconciliationRun.attempt_count + 1,
                claimed_at=now,
                claim_expires_at=expires_at,
                updated_at=now,
                last_error_code=None,
                last_error_at=None,
            )
            .returning(PlatformReconciliationRun)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_run_snapshot(row), claimed=True)
        current = await self.get_by_id(run_id)
        if current is None:
            raise ReconciliationRunNotFound("Reconciliation run was not found for claim")
        return current

    async def assert_claim_owned(self, run_id: uuid.UUID, *, expected_attempt_count: int) -> ReconciliationRunSnapshot:
        result = await self._session.execute(
            select(PlatformReconciliationRun)
            .where(
                PlatformReconciliationRun.id == run_id,
                PlatformReconciliationRun.status == "running",
                PlatformReconciliationRun.claim_state == "processing",
                PlatformReconciliationRun.attempt_count == expected_attempt_count,
            )
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ReconciliationRunClaimLost("Reconciliation run claim was lost")
        return _run_snapshot(row)

    async def mark_running_idle(
        self,
        run_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        watermark_json: dict,
        last_error_code: str | None = None,
        now: datetime,
    ) -> ReconciliationRunSnapshot:
        values = {
            "claim_state": "idle",
            "claimed_at": None,
            "claim_expires_at": None,
            "watermark_json": watermark_json,
            "last_error_code": last_error_code,
            "last_error_at": now if last_error_code else None,
            "updated_at": now,
        }
        return await self._mark(run_id, expected_attempt_count=expected_attempt_count, values=values)

    async def finalize_from_items(
        self,
        run_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        watermark_json: dict,
        now: datetime,
    ) -> ReconciliationRunSnapshot:
        counts = await self.item_counts(run_id)
        unresolved = counts["open"]
        failed = counts["failed"]
        status = "running"
        completed_at = None
        if unresolved == 0:
            status = "failed" if failed else "succeeded"
            completed_at = now
        values = {
            "status": status,
            "claim_state": "idle",
            "claimed_at": None,
            "claim_expires_at": None,
            "watermark_json": watermark_json,
            "scanned_count": counts["total"],
            "discrepancy_count": counts["total"],
            "resolved_count": counts["resolved"] + counts["ignored"],
            "failed_count": failed,
            "completed_at": completed_at,
            "last_error_code": "reconciliation_items_failed" if failed else None,
            "last_error_at": now if failed else None,
            "updated_at": now,
        }
        return await self._mark(run_id, expected_attempt_count=expected_attempt_count, values=values)

    async def record_failure(
        self,
        run_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        last_error_code: str,
        now: datetime,
    ) -> ReconciliationRunSnapshot:
        values = {
            "claim_state": "idle",
            "claimed_at": None,
            "claim_expires_at": None,
            "last_error_code": last_error_code,
            "last_error_at": now,
            "updated_at": now,
        }
        return await self._mark(run_id, expected_attempt_count=expected_attempt_count, values=values)

    async def _mark(
        self,
        run_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        values: dict,
    ) -> ReconciliationRunSnapshot:
        statement = (
            update(PlatformReconciliationRun)
            .where(
                PlatformReconciliationRun.id == run_id,
                PlatformReconciliationRun.claim_state == "processing",
                PlatformReconciliationRun.attempt_count == expected_attempt_count,
            )
            .values(**values)
            .returning(PlatformReconciliationRun)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise ReconciliationRunClaimLost("Reconciliation run claim was lost")
        await self._session.flush()
        return _run_snapshot(row)

    async def item_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        result = await self._session.execute(
            select(
                PlatformReconciliationItem.resolution_status,
                func.count(PlatformReconciliationItem.id),
            )
            .where(PlatformReconciliationItem.reconciliation_run_id == run_id)
            .group_by(PlatformReconciliationItem.resolution_status)
        )
        counts = {"open": 0, "resolved": 0, "ignored": 0, "failed": 0, "total": 0}
        for status, count in result:
            counts[status] = int(count)
            counts["total"] += int(count)
        return counts


class PlatformReconciliationItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def set_tenant_context(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )

    async def discover(
        self,
        *,
        run_id: uuid.UUID,
        organization_id: uuid.UUID,
        evidence: ProviderOperationEvidence,
        discrepancy_classification: str,
        local_object_id: uuid.UUID | None,
    ) -> ReconciliationItemSnapshot:
        statement = (
            insert(PlatformReconciliationItem)
            .values(
                reconciliation_run_id=run_id,
                organization_id=organization_id,
                provider_object_type="provider_operation",
                external_object_ref=evidence.external_operation_ref,
                local_object_type="provider_operation" if local_object_id else None,
                local_object_id=local_object_id,
                discrepancy_classification=discrepancy_classification,
                resolution_status="open",
                claim_state="idle",
                attempt_count=0,
                evidence_sha256=evidence.evidence_sha256,
                evidence_ref=evidence.evidence_ref,
            )
            .on_conflict_do_nothing(constraint="uq_platform_reconciliation_items_run_discrepancy")
            .returning(PlatformReconciliationItem)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_item_snapshot(row), was_created=True)
        existing = await self.get_by_identity(
            run_id=run_id,
            provider_object_type="provider_operation",
            external_object_ref=evidence.external_operation_ref,
            discrepancy_classification=discrepancy_classification,
            for_update=True,
        )
        if existing is None:
            raise ReconciliationItemNotFound("Reconciliation item conflicted but no existing row was visible")
        return existing

    async def get_by_identity(
        self,
        *,
        run_id: uuid.UUID,
        provider_object_type: str,
        external_object_ref: str,
        discrepancy_classification: str,
        for_update: bool = False,
    ) -> ReconciliationItemSnapshot | None:
        statement = select(PlatformReconciliationItem).where(
            PlatformReconciliationItem.reconciliation_run_id == run_id,
            PlatformReconciliationItem.provider_object_type == provider_object_type,
            PlatformReconciliationItem.external_object_ref == external_object_ref,
            PlatformReconciliationItem.discrepancy_classification == discrepancy_classification,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _item_snapshot(row) if row is not None else None

    async def get_by_id(self, item_id: uuid.UUID, *, for_update: bool = False) -> ReconciliationItemSnapshot | None:
        statement = select(PlatformReconciliationItem).where(PlatformReconciliationItem.id == item_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _item_snapshot(row) if row is not None else None

    async def list_open_for_run(self, run_id: uuid.UUID) -> list[ReconciliationItemSnapshot]:
        result = await self._session.execute(
            select(PlatformReconciliationItem)
            .where(
                PlatformReconciliationItem.reconciliation_run_id == run_id,
                PlatformReconciliationItem.resolution_status == "open",
            )
            .order_by(PlatformReconciliationItem.created_at, PlatformReconciliationItem.id)
        )
        return [_item_snapshot(row) for row in result.scalars()]

    async def claim(self, item_id: uuid.UUID, *, now: datetime, expires_at: datetime) -> ReconciliationItemSnapshot:
        statement = (
            update(PlatformReconciliationItem)
            .where(
                PlatformReconciliationItem.id == item_id,
                PlatformReconciliationItem.resolution_status == "open",
                (
                    (PlatformReconciliationItem.claim_state == "idle")
                    | (
                        (PlatformReconciliationItem.claim_state == "processing")
                        & (PlatformReconciliationItem.claim_expires_at < now)
                    )
                ),
            )
            .values(
                claim_state="processing",
                attempt_count=PlatformReconciliationItem.attempt_count + 1,
                claimed_at=now,
                claim_expires_at=expires_at,
                updated_at=now,
                last_error_code=None,
                last_error_at=None,
            )
            .returning(PlatformReconciliationItem)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_item_snapshot(row), claimed=True)
        current = await self.get_by_id(item_id)
        if current is None:
            raise ReconciliationItemNotFound("Reconciliation item was not found for claim")
        return current

    async def assert_claim_owned(self, item_id: uuid.UUID, *, expected_attempt_count: int) -> ReconciliationItemSnapshot:
        result = await self._session.execute(
            select(PlatformReconciliationItem)
            .where(
                PlatformReconciliationItem.id == item_id,
                PlatformReconciliationItem.claim_state == "processing",
                PlatformReconciliationItem.attempt_count == expected_attempt_count,
            )
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ReconciliationClaimLost("Reconciliation item claim was lost")
        return _item_snapshot(row)

    async def resolve(
        self,
        item_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        resolution_status: str,
        last_error_code: str | None,
        now: datetime,
    ) -> ReconciliationItemSnapshot:
        values = {
            "resolution_status": resolution_status,
            "claim_state": "idle",
            "claimed_at": None,
            "claim_expires_at": None,
            "last_error_code": last_error_code,
            "last_error_at": now if last_error_code else None,
            "resolved_at": now,
            "updated_at": now,
        }
        return await self._mark(item_id, expected_attempt_count=expected_attempt_count, values=values)

    async def retryable_failure(
        self,
        item_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        last_error_code: str,
        now: datetime,
    ) -> ReconciliationItemSnapshot:
        values = {
            "claim_state": "idle",
            "claimed_at": None,
            "claim_expires_at": None,
            "last_error_code": last_error_code,
            "last_error_at": now,
            "updated_at": now,
        }
        return await self._mark(item_id, expected_attempt_count=expected_attempt_count, values=values)

    async def _mark(
        self,
        item_id: uuid.UUID,
        *,
        expected_attempt_count: int,
        values: dict,
    ) -> ReconciliationItemSnapshot:
        statement = (
            update(PlatformReconciliationItem)
            .where(
                PlatformReconciliationItem.id == item_id,
                PlatformReconciliationItem.claim_state == "processing",
                PlatformReconciliationItem.attempt_count == expected_attempt_count,
            )
            .values(**values)
            .returning(PlatformReconciliationItem)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise ReconciliationClaimLost("Reconciliation item claim was lost")
        await self._session.flush()
        return _item_snapshot(row)


def _run_snapshot(row: PlatformReconciliationRun) -> ReconciliationRunSnapshot:
    return ReconciliationRunSnapshot(
        id=row.id,
        provider_code=row.provider_code,
        run_identity=row.run_identity,
        status=row.status,
        claim_state=row.claim_state,
        scope_json=dict(row.scope_json),
        watermark_json=dict(row.watermark_json),
        attempt_count=row.attempt_count,
        claimed_at=row.claimed_at,
        claim_expires_at=row.claim_expires_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        last_error_code=row.last_error_code,
        last_error_at=row.last_error_at,
        scanned_count=row.scanned_count,
        discrepancy_count=row.discrepancy_count,
        resolved_count=row.resolved_count,
        failed_count=row.failed_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _item_snapshot(row: PlatformReconciliationItem) -> ReconciliationItemSnapshot:
    return ReconciliationItemSnapshot(
        id=row.id,
        reconciliation_run_id=row.reconciliation_run_id,
        organization_id=row.organization_id,
        provider_object_type=row.provider_object_type,
        external_object_ref=row.external_object_ref,
        local_object_type=row.local_object_type,
        local_object_id=row.local_object_id,
        discrepancy_classification=row.discrepancy_classification,
        resolution_status=row.resolution_status,
        claim_state=row.claim_state,
        attempt_count=row.attempt_count,
        claimed_at=row.claimed_at,
        claim_expires_at=row.claim_expires_at,
        evidence_sha256=row.evidence_sha256,
        evidence_ref=row.evidence_ref,
        last_error_code=row.last_error_code,
        last_error_at=row.last_error_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
    )
