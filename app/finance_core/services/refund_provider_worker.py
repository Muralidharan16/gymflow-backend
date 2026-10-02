from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import update_session_context
from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    RefundProvider,
)
from app.finance_core.services.refund_provider_execution import (
    FinanceRefundProviderExecutionService,
    RefundProviderExecutionClaim,
)
from app.payment_activation.authority import (
    DurableActivationAuthority,
    ProviderAdmission,
)
from app.payment_activation.domain import ActivationCapability


RefundWorkerFaultPoint = Literal[
    "after_claim_commit",
    "after_bind_commit",
    "after_admission_start_commit",
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


RefundActivationAuthorityFactory = Callable[
    [AsyncSession],
    DurableActivationAuthority,
]
RefundSessionContextInstaller = Callable[..., Awaitable[None]]
RefundAdmissionStartDisposition = Literal[
    "provider_execution_permitted",
    "known_no_effect",
    "reconciliation_required",
    "completed_inconsistent",
]

PAY24B_REFUND_ADMISSION_LEASE_SECONDS = 300


@dataclass(frozen=True)
class RefundProviderWorkerResult:
    state: str
    command_id: uuid.UUID | None = None
    provider_called: bool = False
    reclaimed_existing_attempt: bool = False
    replayed_evidence: bool = False


@dataclass(frozen=True)
class RefundAdmissionStartDecision:
    admission: ProviderAdmission
    state: str
    disposition: RefundAdmissionStartDisposition
    provider_execution_permitted: bool


class RefundProviderExecutionProcessor:
    """PAY-10-E/PAY-24-B crash-safe provider execution orchestration.

    PAY-10 remains the durable refund command authority.  PAY-24 admission is
    bound to the exact PAY-10 request and attempt before provider I/O.  Claim,
    request/admission, admission start, and terminal acknowledgement are short,
    separately committed transactions; provider I/O runs with no open database
    transaction.

    A reclaimed PAY-10 processing attempt reuses the same PAY-24 logical
    operation.  If its admission is already active or unknown, the processor
    converges both authorities to unknown and never submits the refund again.
    Financial refund finalization remains a separate PAY-10-D capability.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider_resolver: RefundProviderResolver,
        fault_hook: RefundWorkerFaultHook | None = None,
        activation_authority_factory: RefundActivationAuthorityFactory = (
            DurableActivationAuthority
        ),
        session_context_installer: RefundSessionContextInstaller = (
            update_session_context
        ),
    ):
        self._session_factory = session_factory
        self._provider_resolver = provider_resolver
        self._fault_hook = fault_hook
        self._activation_authority_factory = activation_authority_factory
        self._session_context_installer = session_context_installer

    def _fault(
        self,
        point: RefundWorkerFaultPoint,
        claim: RefundProviderExecutionClaim,
    ) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, claim)

    async def _install_context(
        self,
        session: AsyncSession,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim | None = None,
    ) -> None:
        await self._session_context_installer(
            session,
            org_id=(str(claim.organization_id) if claim else None),
            trace_id=(f"pay24b-refund:{claim.command_id}" if claim else None),
            role="finance_refund_runtime",
            internal_maintenance="true",
            worker_id=str(worker_id),
        )

    async def _claim_one(
        self,
        *,
        worker_id: uuid.UUID,
    ) -> RefundProviderExecutionClaim | None:
        async with self._session_factory() as session:
            try:
                await self._install_context(session, worker_id=worker_id)
                service = FinanceRefundProviderExecutionService(session)
                claims = await service.claim(worker_id=worker_id, limit=1)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return claims[0] if claims else None

    async def _bind_and_request_admission(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        provider: RefundProvider,
    ) -> ProviderAdmission:
        async with self._session_factory() as session:
            try:
                await self._install_context(
                    session,
                    worker_id=worker_id,
                    claim=claim,
                )
                service = FinanceRefundProviderExecutionService(session)
                binding = await service.bind_request(
                    claim=claim,
                    worker_id=worker_id,
                    environment=provider.environment,
                )
                admission_binding = service.provider_admission_binding(
                    claim=claim,
                    binding=binding,
                )
                authority = self._activation_authority_factory(session)
                admission = await authority.request_current_refund_admission(
                    capability=ActivationCapability.REFUND_EXECUTION,
                    logical_operation_id=(
                        admission_binding.logical_operation_id
                    ),
                    operation_sha=admission_binding.operation_sha,
                    lease_seconds=PAY24B_REFUND_ADMISSION_LEASE_SECONDS,
                )
                if (
                    admission.organization_id != claim.organization_id
                    or admission.capability
                    != ActivationCapability.REFUND_EXECUTION
                    or admission.logical_operation_id
                    != admission_binding.logical_operation_id
                ):
                    raise FinanceProviderConfigError(
                        "PAY-24 refund admission differs from PAY-10 authority."
                    )
                # P1: the exact PAY-10 request and current-generation refund
                # admission become durable together before admission start.
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return admission

    async def _start_admission(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        admission: ProviderAdmission,
    ) -> RefundAdmissionStartDecision:
        async with self._session_factory() as session:
            try:
                await self._install_context(
                    session,
                    worker_id=worker_id,
                    claim=claim,
                )
                authority = self._activation_authority_factory(session)
                started = await authority.start_provider_admission(
                    admission_id=admission.admission_id,
                    # The admission UUID is stable across a same-attempt
                    # reclaim and globally unique across retry admissions.
                    execution_id=admission.admission_id,
                )

                # P2 authorization is decided while the start transaction is
                # still open.  Only a decision derived from ``active`` may
                # permit provider I/O after this transaction commits.  PAY-24
                # can legitimately transition an admitted lease to expired in
                # this call, so known terminal states are committed rather
                # than blindly rolled back.
                started_state = started.state
                if started_state == "active":
                    disposition: RefundAdmissionStartDisposition = (
                        "provider_execution_permitted"
                    )
                    provider_execution_permitted = True
                elif started_state in {"expired", "revoked"}:
                    disposition = "known_no_effect"
                    provider_execution_permitted = False
                elif started_state == "unknown":
                    disposition = "reconciliation_required"
                    provider_execution_permitted = False
                elif started_state == "completed":
                    disposition = "completed_inconsistent"
                    provider_execution_permitted = False
                else:
                    raise FinanceProviderConfigError(
                        "PAY-24 refund admission start returned an "
                        "unsupported state."
                    )

                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return RefundAdmissionStartDecision(
            admission=started,
            state=started_state,
            disposition=disposition,
            provider_execution_permitted=provider_execution_permitted,
        )

    async def _record_pay10_error_only(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        error: FinanceProviderOperationError,
    ) -> str:
        """Record a pre-provider or already-terminal admission condition."""

        async with self._session_factory() as session:
            try:
                await self._install_context(
                    session,
                    worker_id=worker_id,
                    claim=claim,
                )
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

    async def _record_provider_error(
        self,
        *,
        worker_id: uuid.UUID,
        claim: RefundProviderExecutionClaim,
        error: FinanceProviderOperationError,
        admission: ProviderAdmission,
    ) -> str:
        async with self._session_factory() as session:
            try:
                await self._install_context(
                    session,
                    worker_id=worker_id,
                    claim=claim,
                )
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
                authority = self._activation_authority_factory(session)
                await authority.finish_provider_admission(
                    admission_id=admission.admission_id,
                    execution_id=admission.admission_id,
                    outcome=(
                        "unknown"
                        if error.requires_reconciliation
                        else "completed"
                    ),
                )
                # P3: PAY-10 and PAY-24 terminal outcomes commit atomically.
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
        admission: ProviderAdmission,
    ):
        async with self._session_factory() as session:
            try:
                await self._install_context(
                    session,
                    worker_id=worker_id,
                    claim=claim,
                )
                service = FinanceRefundProviderExecutionService(session)
                receipt = await service.record_outcome(
                    claim=claim,
                    worker_id=worker_id,
                    response=response,
                )
                authority = self._activation_authority_factory(session)
                await authority.finish_provider_admission(
                    admission_id=admission.admission_id,
                    execution_id=admission.admission_id,
                    outcome="completed",
                )
                # P3: provider evidence and admission completion are one
                # refund-runtime transaction. Financial finalization is not.
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
        except FinanceProviderConfigError:
            error = FinanceProviderOperationError(
                provider_code=claim.provider_code,
                operation="submit_refund",
                code="PROVIDER_CONFIG_UNAVAILABLE",
                failure_class="retryable",
                message="Refund provider configuration is unavailable.",
            )
            state = await self._record_pay10_error_only(
                worker_id=worker_id,
                claim=claim,
                error=error,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        # P1 denial (including Stage 0) rolls back request binding/admission and
        # propagates with the PAY-10 lease intact.  No provider result is
        # manufactured before any provider I/O.
        admission = await self._bind_and_request_admission(
            worker_id=worker_id,
            claim=claim,
            provider=provider,
        )
        self._fault("after_bind_commit", claim)

        if admission.state in {"active", "unknown"}:
            ambiguous = FinanceProviderOperationError(
                provider_code=claim.provider_code,
                operation="submit_refund",
                code="PAY24_REFUND_ADMISSION_ALREADY_STARTED",
                failure_class="unknown",
                message=(
                    "Refund admission may already have produced a provider "
                    "effect; reconciliation is required."
                ),
            )
            state = await self._record_provider_error(
                worker_id=worker_id,
                claim=claim,
                error=ambiguous,
                admission=admission,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        if admission.state == "completed":
            # Atomic P3 makes this state unreachable while PAY-10 is still
            # processing.  Stop the command for reconciliation without trying
            # to rewrite an already completed PAY-24 admission.
            inconsistent = FinanceProviderOperationError(
                provider_code=claim.provider_code,
                operation="submit_refund",
                code="PAY24_REFUND_COMPLETED_WITHOUT_PAY10_OUTCOME",
                failure_class="unknown",
                message="Refund admission completion conflicts with PAY-10 state.",
            )
            state = await self._record_pay10_error_only(
                worker_id=worker_id,
                claim=claim,
                error=inconsistent,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        if admission.state in {"expired", "revoked"}:
            no_effect = FinanceProviderOperationError(
                provider_code="activation",
                operation="submit_refund",
                code=f"PAY24_REFUND_ADMISSION_{admission.state.upper()}",
                failure_class="retryable",
                message="Refund admission ended before provider I/O.",
            )
            state = await self._record_pay10_error_only(
                worker_id=worker_id,
                claim=claim,
                error=no_effect,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        if admission.state != "admitted":
            raise RuntimeError(
                f"Unsupported PAY-24 refund admission state: {admission.state}"
            )

        # Any start failure is pre-provider and propagates after rollback.  The
        # PAY-10 lease remains durable for safe reclaim; no false provider
        # success/failure evidence is manufactured.
        start_decision = await self._start_admission(
            worker_id=worker_id,
            claim=claim,
            admission=admission,
        )
        self._fault("after_admission_start_commit", claim)

        if start_decision.disposition == "known_no_effect":
            no_effect = FinanceProviderOperationError(
                provider_code="activation",
                operation="submit_refund",
                code=(
                    "PAY24_REFUND_ADMISSION_"
                    f"{start_decision.state.upper()}"
                ),
                failure_class="retryable",
                message="Refund admission was not active before provider I/O.",
            )
            state = await self._record_pay10_error_only(
                worker_id=worker_id,
                claim=claim,
                error=no_effect,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        if start_decision.disposition in {
            "reconciliation_required",
            "completed_inconsistent",
        }:
            inconsistent = FinanceProviderOperationError(
                provider_code=claim.provider_code,
                operation="submit_refund",
                code=(
                    "PAY24_REFUND_ADMISSION_UNKNOWN"
                    if start_decision.disposition
                    == "reconciliation_required"
                    else "PAY24_REFUND_COMPLETED_WITHOUT_PAY10_OUTCOME"
                ),
                failure_class="unknown",
                message=(
                    "Refund admission start returned a terminal state that "
                    "does not safely agree with PAY-10 authority."
                ),
            )
            state = await self._record_pay10_error_only(
                worker_id=worker_id,
                claim=claim,
                error=inconsistent,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        if not start_decision.provider_execution_permitted:
            raise RuntimeError(
                "PAY-24 refund admission start decision is unsupported."
            )

        try:
            response = await provider.submit_refund(claim.provider_request())
        except FinanceProviderOperationError as error:
            state = await self._record_provider_error(
                worker_id=worker_id,
                claim=claim,
                error=error,
                admission=admission,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                provider_called=True,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        self._fault("after_provider_effect", claim)

        try:
            receipt = await self._record_outcome(
                worker_id=worker_id,
                claim=claim,
                response=response,
                admission=admission,
            )
        except FinanceProviderOperationError as error:
            # Service-level response validation occurs after provider I/O.  A
            # mismatch is therefore ambiguous, not a harmless local failure.
            state = await self._record_provider_error(
                worker_id=worker_id,
                claim=claim,
                error=error,
                admission=admission,
            )
            return RefundProviderWorkerResult(
                state=state,
                command_id=claim.command_id,
                provider_called=True,
                reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            )

        self._fault("after_outcome_commit", claim)

        return RefundProviderWorkerResult(
            state=receipt.command_status,
            command_id=claim.command_id,
            provider_called=True,
            reclaimed_existing_attempt=claim.reclaimed_existing_attempt,
            replayed_evidence=receipt.replayed,
        )
