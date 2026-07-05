from __future__ import annotations

from collections.abc import Callable

import uuid

from dataclasses import replace
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.provider_operations import (
    ProviderCallResult,
    ProviderOperationRequest,
    ProviderOperationResult,
    ProviderOperationSnapshot,
    ProviderOutcomeKind,
    ProviderResultPersistenceFailure,
    provider_call_request_from_operation,
    result_for_outcome,
)
from app.platform_billing.providers.base import PlatformBillingProvider
from app.platform_billing.repositories.provider_operations import (
    PlatformProviderOperationRepository,
)


SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]


class PlatformProviderOperationService:
    def __init__(
        self,
        provider: PlatformBillingProvider,
        *,
        session_factory: SessionFactory = AsyncSessionLocal,
    ):
        self._provider = provider
        self._session_factory = session_factory

    async def execute(
        self,
        request: ProviderOperationRequest,
    ) -> ProviderOperationResult:
        reserved = await self.reserve_operation(request)
        if not reserved.execution_claimed:
            return _result_from_snapshot(reserved, provider_called=False)

        call_request = provider_call_request_from_operation(
            operation_id=reserved.id,
            request=request,
        )
        try:
            provider_result = await self._provider.execute(call_request)
        except Exception:
            provider_result = ProviderCallResult(
                outcome=ProviderOutcomeKind.UNKNOWN,
                error_classification="provider_unexpected_exception",
            )
        operation_result = result_for_outcome(reserved.id, provider_result)
        return await self.record_result(
            organization_id=request.organization_id,
            result=operation_result,
        )

    async def reserve_operation(
        self,
        request: ProviderOperationRequest,
    ) -> ProviderOperationSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformProviderOperationRepository(session)
                await repository.set_tenant_context(request.organization_id)
                reserved = await repository.reserve(request)
                if reserved.status != "reserved":
                    return reserved
                claimed = await repository.claim_for_execution(reserved.id)
                return replace(claimed, was_created=reserved.was_created)

    async def record_result(
        self,
        *,
        organization_id: uuid.UUID,
        result: ProviderOperationResult,
    ) -> ProviderOperationResult:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = PlatformProviderOperationRepository(session)
                    await repository.set_tenant_context(organization_id)
                    snapshot = await repository.record_result(result)
                    return _result_from_snapshot(snapshot, provider_called=result.provider_called)
        except SQLAlchemyError as exc:
            raise ProviderResultPersistenceFailure("Provider result could not be persisted") from exc


def _result_from_snapshot(
    snapshot: ProviderOperationSnapshot,
    *,
    provider_called: bool,
) -> ProviderOperationResult:
    return ProviderOperationResult(
        operation_id=snapshot.id,
        status=snapshot.status,
        external_operation_ref=snapshot.external_operation_ref,
        error_classification=snapshot.error_classification,
        result_evidence_sha256=snapshot.result_evidence_sha256,
        result_reference=snapshot.result_reference,
        provider_called=provider_called,
    )
