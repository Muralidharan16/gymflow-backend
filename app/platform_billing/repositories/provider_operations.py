from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_billing.domain.provider_operations import (
    IdempotencyConflict,
    ProviderOperationNotFound,
    ProviderOperationResult,
    ProviderOperationSnapshot,
    compute_provider_request_hash,
    ensure_legal_transition,
    utc_now,
)
from app.platform_billing.domain.provider_operations import ProviderOperationRequest
from app.platform_billing.models.provider import PlatformProviderOperation


class PlatformProviderOperationRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def set_tenant_context(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )

    async def reserve(
        self,
        request: ProviderOperationRequest,
    ) -> ProviderOperationSnapshot:
        request_hash = compute_provider_request_hash(request)
        statement = (
            insert(PlatformProviderOperation)
            .values(
                organization_id=request.organization_id,
                provider_code=request.provider_code,
                operation_type=request.operation_type,
                idempotency_key=request.idempotency_key,
                canonical_request_sha256=request_hash,
                status="reserved",
                attempt_count=0,
            )
            .on_conflict_do_nothing(
                constraint="uq_platform_provider_operations_idempotency",
            )
            .returning(PlatformProviderOperation)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_snapshot(row), was_created=True)

        existing = await self.get_by_idempotency(
            organization_id=request.organization_id,
            provider_code=request.provider_code,
            idempotency_key=request.idempotency_key,
            for_update=True,
        )
        if existing is None:
            raise ProviderOperationNotFound("Provider operation reservation was not found after conflict")
        if existing.canonical_request_sha256 != request_hash:
            raise IdempotencyConflict("Idempotency key already exists for a different provider operation request")
        return existing

    async def claim_for_execution(
        self,
        operation_id: uuid.UUID,
    ) -> ProviderOperationSnapshot:
        statement = (
            update(PlatformProviderOperation)
            .where(
                PlatformProviderOperation.id == operation_id,
                PlatformProviderOperation.status == "reserved",
            )
            .values(
                status="in_progress",
                attempt_count=PlatformProviderOperation.attempt_count + 1,
            )
            .returning(PlatformProviderOperation)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.flush()
            return replace(_snapshot(row), execution_claimed=True)

        current = await self.get_by_id(operation_id)
        if current is None:
            raise ProviderOperationNotFound("Provider operation was not found for execution claim")
        return current

    async def get_by_idempotency(
        self,
        *,
        organization_id: uuid.UUID,
        provider_code: str,
        idempotency_key: str,
        for_update: bool = False,
    ) -> ProviderOperationSnapshot | None:
        statement = select(PlatformProviderOperation).where(
            PlatformProviderOperation.organization_id == organization_id,
            PlatformProviderOperation.provider_code == provider_code,
            PlatformProviderOperation.idempotency_key == idempotency_key,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _snapshot(row) if row is not None else None

    async def get_by_id(
        self,
        operation_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ProviderOperationSnapshot | None:
        statement = select(PlatformProviderOperation).where(
            PlatformProviderOperation.id == operation_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return _snapshot(row) if row is not None else None

    async def record_result(
        self,
        result: ProviderOperationResult,
    ) -> ProviderOperationSnapshot:
        row_result = await self._session.execute(
            select(PlatformProviderOperation)
            .where(PlatformProviderOperation.id == result.operation_id)
            .with_for_update()
        )
        row = row_result.scalar_one_or_none()
        if row is None:
            raise ProviderOperationNotFound("Provider operation was not found")

        if row.status == result.status and row.status in {"succeeded", "failed", "unknown"}:
            return _snapshot(row)
        ensure_legal_transition(row.status, result.status)
        row.status = result.status
        row.external_operation_ref = result.external_operation_ref or row.external_operation_ref
        row.result_evidence_sha256 = result.result_evidence_sha256
        row.result_reference = result.result_reference
        row.error_classification = result.error_classification
        row.completed_at = utc_now()
        await self._session.flush()
        return _snapshot(row)


def _snapshot(row: PlatformProviderOperation) -> ProviderOperationSnapshot:
    return ProviderOperationSnapshot(
        id=row.id,
        organization_id=row.organization_id,
        provider_code=row.provider_code,
        operation_type=row.operation_type,
        idempotency_key=row.idempotency_key,
        canonical_request_sha256=row.canonical_request_sha256,
        status=row.status,
        external_operation_ref=row.external_operation_ref,
        attempt_count=row.attempt_count,
        result_evidence_sha256=row.result_evidence_sha256,
        result_reference=row.result_reference,
        error_classification=row.error_classification,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        was_created=False,
        execution_claimed=False,
    )
