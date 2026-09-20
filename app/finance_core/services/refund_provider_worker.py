from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    RefundProvider,
)
from app.finance_core.services.refund_provider_execution import (
    FinanceRefundProviderExecutionService,
    RefundProviderExecutionClaim,
    refund_provider_evidence_hash,
)


RefundWorkerFaultPoint = Literal[
    "after_claim_commit",
    "after_bind_commit",
    "after_provider_effect",
    "after_outcome_commit",
]
RefundWorkerFaultHook = Callable[
    [RefundWorkerFaultPoint, RefundProviderExecutionClaim],
    None,
]


class RefundProviderResolver(Protocol):
    def __call__(self, provider_code: str) -> RefundProvider:
        ...


@dataclass(frozen=True)
class RefundProviderWorkerResult:
    state: str
    command_id: uuid.UUID | None = None
    provider_called: bool = False
    reclaimed_existing_attempt: bool = False
    replayed_evidence: bool = False


class RefundProviderExecutionProcessor:
    """PAY-10-E crash-safe provider execution orchestration.

    Durable PostgreSQL state is the only business authority. Claim and request
    binding are committed before provider I/O. Provider I/O runs with no open
    database transaction. Provider outcome acknowledgement is then committed in
    a fresh transaction.

    Any unexpected exception after provider I/O is intentionally propagated.
    The durable lease/request identity and deterministic provider identity make
    redelivery/reconciliation safe; the processor never manufactures success
    from task completion, HTTP submission, or broker acknowledgement.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider_resolver: RefundProviderResolver,
        fault_hook: RefundWorkerFaultHook | None = None,
    ):
        self._session_factory = session_factory
        self._provider_resolver = provider_resolver
        self._fault_hook = fault_hook

    def _fault(
        self,
        point: RefundWorkerFaultPoint,
        claim: RefundProviderExecutionClaim,
    ) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, claim)

    async def _claim_one(
        self,
        *,
        worker_id: uuid.UUID,
    ) -> RefundProviderExecutionClaim | None:
        async with self._session_factory() as session:
            try:
                service = FinanceRefundProviderExecutionService(session)
                claims = await service.claim(worker_id=worker_id, limit=1)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return claims[0] if claims else None

    async def _bind(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        provider: RefundProvider,
    ) -> None:
        async with self._session_factory() as session:
            try:
                service = FinanceRefundProviderExecutionService(session)
                await service.bind_request(
                    claim=claim,
                    worker_id=worker_id,
                    environment=provider.environment,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def _record_provider_error(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        error: FinanceProviderOperationError,
    ) -> str:
        async with self._session_factory() as session:
            try:
                service = FinanceRefundProviderExecutionService(session)
                if error.requires_reconciliation:
                    state = await service.record_unknown(
                        claim=claim,
                        worker_id=worker_id,
                        error=error,
                    )
                else:
                    state = await service.record_failure(
                        claim=claim,
                        worker_id=worker_id,
                        error=error,
                        permanent=not error.automatic_retry_allowed,
                    )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return state

    async def _record_outcome(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        response,
    ):
        evidence_sha256 = refund_provider_evidence_hash(
            response,
            source="submission",
        )
        async with self._session_factory() as session:
            try:
                service = FinanceRefundProviderExecutionService(session)
                receipt = await service.record_outcome(
                    claim=claim,
                    worker_id=worker_id,
                    response=response,
                    evidence_sha256=evidence_sha256,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return receipt

    async def run_once(
        self,
        *,
        worker_id: uuid.UUID,
    ) -> RefundProviderWorkerResult:
        claim = await self._claim_one(worker_id=worker_id)
        if claim is None:
            return RefundProviderWorkerResult(state="idle")

        self._fault("after_claim_commit", claim)

        try:
            provider = self._provider_resolver(claim.provider_code)
        except FinanceProviderConfigError as exc:
            error = FinanceProviderOperationError(
                provider_code=claim.provider_code,
                operation="submit_refund",
                code="PROVIDER_CONFIG_UNAVAILABLE",
                failure_class="retryable",
                message="Refund provider configuration is unavailable.",
            )
            state = await self._record_provider_error(
                worker_id=worker_id,
                claim=claim,
                error=error,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        await self._bind(
            worker_id=worker_id,
            claim=claim,
            provider=provider,
        )
        self._fault("after_bind_commit", claim)

        try:
            response = await provider.submit_refund(claim.provider_request())
        except FinanceProviderOperationError as error:
            state = await self._record_provider_error(
                worker_id=worker_id,
                claim=claim,
                error=error,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                provider_called=True,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        self._fault("after_provider_effect", claim)

        receipt = await self._record_outcome(
            worker_id=worker_id,
            claim=claim,
            response=response,
        )

        self._fault("after_outcome_commit", claim)

        return RefundProviderWorkerResult(
            state=receipt.command_status,
            command_id=claim.command_id,
            provider_called=True,
            reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            replayed_evidence=receipt.replayed,
        )
