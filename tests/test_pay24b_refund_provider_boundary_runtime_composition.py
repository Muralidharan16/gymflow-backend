from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.exc import DBAPIError

import app.finance_core.services.refund_provider_worker as refund_worker
from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderRefundResponse,
)
from app.finance_core.services.refund_provider_execution import (
    FinanceRefundProviderExecutionService as _RealExecutionService,
    RefundProviderEvidenceReceipt,
    RefundProviderExecutionClaim,
    RefundProviderRequestBinding,
)
from app.payment_activation.domain import ActivationCapability


class _Session:
    def __init__(self, scenario: "_Scenario", number: int) -> None:
        self.scenario = scenario
        self.number = number
        self.name = f"refund-{number}"
        self.info: dict[object, object] = {}
        self._in_transaction = False

    def touch(self) -> None:
        self._in_transaction = True

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def commit(self) -> None:
        self.scenario.events.append(f"{self.name}:commit")
        self._in_transaction = False

    async def rollback(self) -> None:
        self.scenario.events.append(f"{self.name}:rollback")
        self._in_transaction = False


class _SessionLease:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, exc, traceback
        return False


class _SessionFactory:
    def __init__(self, scenario: "_Scenario") -> None:
        self.scenario = scenario

    def __call__(self) -> _SessionLease:
        session = _Session(self.scenario, len(self.scenario.sessions))
        self.scenario.sessions.append(session)
        return _SessionLease(session)


def _provider_error(
    failure_class: str,
    *,
    code: str | None = None,
) -> FinanceProviderOperationError:
    return FinanceProviderOperationError(
        provider_code="razorpay_sandbox",
        operation="submit_refund",
        code=code or f"PAY24B_REFUND_{failure_class.upper()}",
        failure_class=failure_class,
        message=f"Injected {failure_class} refund provider result.",
    )


class _Scenario:
    def __init__(
        self,
        mode: str,
        *,
        attempt_count: int = 1,
        reclaimed_existing_attempt: bool = False,
        command_id: uuid.UUID | None = None,
    ) -> None:
        self.mode = mode
        self.events: list[str] = []
        self.sessions: list[_Session] = []
        self.contexts: list[tuple[str, dict[str, object]]] = []
        self.requested_logical_operation_ids: list[str] = []
        self.provider_calls = 0
        self.pay10_terminal_session: _Session | None = None
        self.worker_id = uuid.uuid4()
        self.admission_id = uuid.uuid4()
        self.claim = RefundProviderExecutionClaim(
            command_id=command_id or uuid.uuid4(),
            refund_id=uuid.uuid4(),
            payment_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            provider_code="razorpay_sandbox",
            provider_payment_ref="pay_pay24b_refund_composition",
            amount=Decimal("25.00"),
            currency_code="INR",
            attempt_count=attempt_count,
            lease_fence=13,
            reclaimed_existing_attempt=reclaimed_existing_attempt,
            lease_expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        self.binding = RefundProviderRequestBinding(
            command_id=self.claim.command_id,
            refund_id=self.claim.refund_id,
            payment_id=self.claim.payment_id,
            organization_id=self.claim.organization_id,
            provider_code=self.claim.provider_code,
            provider_payment_ref=str(self.claim.provider_payment_ref),
            amount=self.claim.amount,
            currency_code=self.claim.currency_code,
            request_sha256="a" * 64,
            status="processing",
        )
        self.session_factory = _SessionFactory(self)

    def resolve_provider(self, provider_code: str):
        assert provider_code == self.claim.provider_code
        self.events.append("provider:resolve")
        return _Provider(self)


class _Provider:
    provider_code = "razorpay_sandbox"
    environment = "test"

    def __init__(self, scenario: _Scenario) -> None:
        self.scenario = scenario

    async def submit_refund(self, request):
        s = self.scenario
        assert request == s.claim.provider_request()
        assert s.sessions
        assert all(not session.in_transaction() for session in s.sessions)
        assert any(
            context.get("org_id") == str(s.claim.organization_id)
            for _, context in s.contexts
        )

        s.provider_calls += 1
        s.events.append("provider:submit_refund")

        if s.mode in {"retryable", "final", "unknown"}:
            raise _provider_error(s.mode)

        values: dict[str, object] = {
            "provider_code": self.provider_code,
            "provider_refund_ref": "rfnd_pay24b_composition",
            "provider_payment_ref": request.provider_payment_ref,
            "amount": request.amount,
            "currency_code": request.currency_code,
            "receipt": "pay24b-refund-composition-receipt",
            "status": "processed",
        }
        malformed_values: dict[str, tuple[str, object]] = {
            "mismatched_provider_code": (
                "provider_code",
                "different_provider",
            ),
            "malformed_provider_code_type": ("provider_code", None),
            "mismatched_payment_ref": (
                "provider_payment_ref",
                "pay_wrong",
            ),
            "malformed_payment_ref_type": ("provider_payment_ref", None),
            "mismatched_amount": ("amount", Decimal("24.99")),
            "malformed_amount_type": ("amount", "25.00"),
            "mismatched_currency": ("currency_code", "USD"),
            "malformed_currency_type": ("currency_code", None),
            "malformed_refund_ref_none": ("provider_refund_ref", None),
            "malformed_refund_ref_type": ("provider_refund_ref", 123),
            "malformed_refund_ref_empty": ("provider_refund_ref", ""),
            "malformed_refund_ref_characters": (
                "provider_refund_ref",
                "rfnd_bad/path",
            ),
            "malformed_status_type": ("status", ["processed"]),
            "malformed_status_value": ("status", "mystery"),
            "malformed_receipt_type": ("receipt", None),
        }
        malformed = malformed_values.get(s.mode)
        if malformed is not None:
            field, value = malformed
            values[field] = value

        return ProviderRefundResponse(**values)

    async def fetch_refund(self, request, *, provider_refund_ref):
        del request, provider_refund_ref
        raise AssertionError("provider reconciliation must not run in execution")


class _ExecutionService:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.scenario = session.scenario

    async def claim(self, *, worker_id: uuid.UUID, limit: int = 1):
        assert worker_id == self.scenario.worker_id
        assert limit == 1
        assert self.session.number == 0
        self.session.touch()
        self.scenario.events.append("pay10:claim")
        return [self.scenario.claim]

    async def bind_request(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        environment: str,
    ) -> RefundProviderRequestBinding:
        assert claim is self.scenario.claim
        assert worker_id == self.scenario.worker_id
        assert environment == "test"
        assert self.session.number == 1
        self.session.touch()
        self.scenario.events.append("pay10:bind_request")
        return self.scenario.binding

    @staticmethod
    def provider_admission_binding(*, claim, binding):
        return _RealExecutionService.provider_admission_binding(
            claim=claim,
            binding=binding,
        )

    async def record_outcome(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        response: ProviderRefundResponse,
        occurred_at=None,
    ) -> RefundProviderEvidenceReceipt:
        del occurred_at
        assert claim is self.scenario.claim
        assert worker_id == self.scenario.worker_id
        self.scenario.events.append("pay10:validate_outcome")

        response = _RealExecutionService.validate_provider_response(
            claim=claim,
            response=response,
        )
        if self.scenario.mode == "programming_error":
            raise RuntimeError("Injected unrelated P3 programming error.")

        self.session.touch()
        self.scenario.pay10_terminal_session = self.session
        self.scenario.events.append("pay10:record_outcome")
        return RefundProviderEvidenceReceipt(
            evidence_id=uuid.uuid4(),
            command_id=claim.command_id,
            normalized_status=response.status,
            command_status="reconciliation_pending",
            replayed=False,
        )

    async def record_unknown(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        error: FinanceProviderOperationError,
    ) -> str:
        assert claim is self.scenario.claim
        assert worker_id == self.scenario.worker_id
        assert error.requires_reconciliation
        self.session.touch()
        self.scenario.pay10_terminal_session = self.session
        self.scenario.events.append("pay10:record_unknown")
        return "reconciliation_pending"

    async def record_failure(
        self,
        *,
        claim: RefundProviderExecutionClaim,
        worker_id: uuid.UUID,
        error: FinanceProviderOperationError,
        permanent: bool,
    ) -> str:
        assert claim is self.scenario.claim
        assert worker_id == self.scenario.worker_id
        assert not error.requires_reconciliation
        assert permanent is (not error.automatic_retry_allowed)
        self.session.touch()
        self.scenario.pay10_terminal_session = self.session
        self.scenario.events.append(
            f"pay10:record_failure:{'final' if permanent else 'retryable'}"
        )
        return "dead_lettered" if permanent else "retry_pending"


class _ObservedStartedAdmission:
    def __init__(self, session: _Session, state: str) -> None:
        self.admission_id = session.scenario.admission_id
        self._session = session
        self._state = state

    @property
    def state(self) -> str:
        # This assertion makes the runtime proof reject state inspection after
        # the P2 transaction has committed or rolled back.
        assert self._session.in_transaction()
        self._session.scenario.events.append(
            f"pay24:inspect_started_state:{self._state}"
        )
        return self._state


class _Authority:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.scenario = session.scenario

    async def request_current_refund_admission(
        self,
        *,
        capability,
        logical_operation_id: str,
        operation_sha: str,
        lease_seconds: int,
    ):
        s = self.scenario
        assert self.session.number == 1
        assert capability is ActivationCapability.REFUND_EXECUTION
        assert logical_operation_id == (
            f"refund:{s.claim.command_id}:{s.claim.attempt_count}"
        )
        assert operation_sha == s.binding.request_sha256
        assert lease_seconds == 300
        assert self.session.info.get("org_id") == str(
            s.claim.organization_id
        )
        assert self.session.info.get("role") == "finance_refund_runtime"
        self.session.touch()
        s.requested_logical_operation_ids.append(logical_operation_id)
        s.events.append("pay24:request_refund_admission")

        if s.mode == "stage0":
            raise DBAPIError(
                "PAY24 refund admission",
                {},
                RuntimeError("Stage 0 refund admission denied"),
            )

        replay_state = {
            "replay_active": "active",
            "replay_unknown": "unknown",
            "replay_completed": "completed",
            "replay_expired": "expired",
            "replay_revoked": "revoked",
        }.get(s.mode)
        return SimpleNamespace(
            admission_id=s.admission_id,
            activation_generation=7,
            organization_id=s.claim.organization_id,
            capability=ActivationCapability.REFUND_EXECUTION,
            logical_operation_id=logical_operation_id,
            state=replay_state or "admitted",
        )

    async def start_provider_admission(
        self,
        *,
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
    ):
        s = self.scenario
        assert admission_id == s.admission_id
        assert execution_id == s.admission_id
        assert self.session.info.get("org_id") == str(
            s.claim.organization_id
        )
        self.session.touch()
        s.events.append("pay24:start_refund_admission")

        if s.mode == "start_denied":
            raise DBAPIError(
                "PAY24 refund admission start",
                {},
                RuntimeError("Refund admission start denied"),
            )
        start_state = {
            "start_expired": "expired",
            "start_revoked": "revoked",
            "start_unknown": "unknown",
            "start_completed": "completed",
            "start_unexpected": "admitted",
        }.get(s.mode, "active")
        return _ObservedStartedAdmission(
            self.session,
            start_state,
        )

    async def finish_provider_admission(
        self,
        *,
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
        outcome: str,
    ):
        s = self.scenario
        assert admission_id == s.admission_id
        assert execution_id == s.admission_id
        assert outcome in {"completed", "unknown"}
        assert s.pay10_terminal_session is self.session
        assert self.session.info.get("org_id") == str(
            s.claim.organization_id
        )
        self.session.touch()
        s.events.append(f"pay24:finish_refund_admission:{outcome}")
        return SimpleNamespace(
            admission_id=s.admission_id,
            state=outcome,
        )


async def _install_refund_context(
    session: _Session,
    **context: object,
) -> None:
    scenario = session.scenario
    assert context.get("role") == "finance_refund_runtime"
    assert context.get("internal_maintenance") == "true"
    assert context.get("worker_id") == str(scenario.worker_id)
    scenario.contexts.append((session.name, dict(context)))
    session.info.update(
        {
            key: value
            for key, value in context.items()
            if value is not None
        }
    )
    scenario.events.append(f"context:{session.name}")


def _authority_factory(session: _Session) -> _Authority:
    return _Authority(session)


async def _execute(
    mode: str,
    *,
    attempt_count: int = 1,
    reclaimed_existing_attempt: bool = False,
    command_id: uuid.UUID | None = None,
):
    scenario = _Scenario(
        mode,
        attempt_count=attempt_count,
        reclaimed_existing_attempt=reclaimed_existing_attempt,
        command_id=command_id,
    )
    processor = refund_worker.RefundProviderExecutionProcessor(
        session_factory=scenario.session_factory,
        provider_resolver=scenario.resolve_provider,
        activation_authority_factory=_authority_factory,
        session_context_installer=_install_refund_context,
    )

    try:
        result = await processor.run_once(worker_id=scenario.worker_id)
    except BaseException as exc:
        return scenario, None, exc
    return scenario, result, None


def _assert_claim_and_p1(scenario: _Scenario) -> None:
    events = scenario.events
    claim = events.index("pay10:claim")
    r0_commit = events.index("refund-0:commit")
    resolve = events.index("provider:resolve")
    bind = events.index("pay10:bind_request")
    request = events.index("pay24:request_refund_admission")
    p1_commit = events.index("refund-1:commit")

    assert claim < r0_commit < resolve < bind < request < p1_commit
    assert not any(
        event.endswith(":commit") for event in events[bind + 1 : request]
    )


def _assert_provider_fences(scenario: _Scenario) -> None:
    events = scenario.events
    start = events.index("pay24:start_refund_admission")
    inspect = events.index("pay24:inspect_started_state:active")
    p2_commit = events.index("refund-2:commit")
    provider = events.index("provider:submit_refund")
    assert start < inspect < p2_commit < provider
    assert all(not session.in_transaction() for session in scenario.sessions)


def _assert_non_active_start_committed_after_inspection(
    scenario: _Scenario,
    *,
    state: str,
) -> None:
    events = scenario.events
    start = events.index("pay24:start_refund_admission")
    inspect = events.index(f"pay24:inspect_started_state:{state}")
    commit = events.index("refund-2:commit")
    assert start < inspect < commit
    assert "provider:submit_refund" not in events


def _assert_pay10_only_terminal_transaction(
    scenario: _Scenario,
    *,
    pay10_event: str,
) -> None:
    events = scenario.events
    pay10 = events.index(pay10_event)
    commit = next(
        index
        for index in range(pay10 + 1, len(events))
        if events[index].endswith(":commit")
    )
    assert pay10 < commit
    assert not any(
        event.startswith("pay24:finish_refund_admission")
        for event in events
    )


def _assert_terminal_transaction(
    scenario: _Scenario,
    *,
    pay10_event: str,
    outcome: str,
) -> None:
    events = scenario.events
    pay10 = events.index(pay10_event)
    finish = events.index(f"pay24:finish_refund_admission:{outcome}")
    commit = next(
        index
        for index in range(finish + 1, len(events))
        if events[index].endswith(":commit")
    )
    assert pay10 < finish < commit
    assert not any(
        event.endswith(":commit") for event in events[pay10 + 1 : finish]
    )
    assert scenario.events[commit].startswith("refund-")


def _assert_claim_derived_context(scenario: _Scenario) -> None:
    assert scenario.contexts
    first_name, first = scenario.contexts[0]
    assert first_name == "refund-0"
    assert first.get("org_id") is None
    assert first.get("trace_id") is None

    for _, context in scenario.contexts[1:]:
        assert context.get("org_id") == str(scenario.claim.organization_id)
        assert context.get("trace_id") == (
            f"pay24b-refund:{scenario.claim.command_id}"
        )
        assert context.get("worker_id") == str(scenario.worker_id)
        assert context.get("role") == "finance_refund_runtime"
        assert context.get("internal_maintenance") == "true"


async def _certify() -> None:
    original_service = refund_worker.FinanceRefundProviderExecutionService
    refund_worker.FinanceRefundProviderExecutionService = _ExecutionService

    try:
        success, result, error = await _execute("success")
        assert error is None
        assert result is not None
        assert result.state == "reconciliation_pending"
        assert result.provider_called is True
        assert success.provider_calls == 1
        assert len(success.sessions) == 4
        assert sum(
            event.endswith(":commit") for event in success.events
        ) == 4
        _assert_claim_and_p1(success)
        _assert_provider_fences(success)
        _assert_terminal_transaction(
            success,
            pay10_event="pay10:record_outcome",
            outcome="completed",
        )
        _assert_claim_derived_context(success)

        for mode, expected_state, failure_event in (
            ("retryable", "retry_pending", "pay10:record_failure:retryable"),
            ("final", "dead_lettered", "pay10:record_failure:final"),
        ):
            failed, result, error = await _execute(mode)
            assert error is None
            assert result is not None
            assert result.state == expected_state
            assert result.provider_called is True
            assert failed.provider_calls == 1
            _assert_claim_and_p1(failed)
            _assert_provider_fences(failed)
            _assert_terminal_transaction(
                failed,
                pay10_event=failure_event,
                outcome="completed",
            )

        unknown, result, error = await _execute("unknown")
        assert error is None
        assert result is not None
        assert result.state == "reconciliation_pending"
        assert result.provider_called is True
        assert unknown.provider_calls == 1
        _assert_terminal_transaction(
            unknown,
            pay10_event="pay10:record_unknown",
            outcome="unknown",
        )

        for mode in (
            "mismatched_provider_code",
            "malformed_provider_code_type",
            "mismatched_payment_ref",
            "malformed_payment_ref_type",
            "mismatched_amount",
            "malformed_amount_type",
            "mismatched_currency",
            "malformed_currency_type",
            "malformed_refund_ref_none",
            "malformed_refund_ref_type",
            "malformed_refund_ref_empty",
            "malformed_refund_ref_characters",
            "malformed_status_type",
            "malformed_status_value",
            "malformed_receipt_type",
        ):
            malformed, result, error = await _execute(mode)
            assert error is None
            assert result is not None
            assert result.state == "reconciliation_pending"
            assert result.provider_called is True
            assert malformed.provider_calls == 1
            assert malformed.events.count("provider:submit_refund") == 1
            assert "pay10:validate_outcome" in malformed.events
            assert "pay10:record_outcome" not in malformed.events
            _assert_provider_fences(malformed)
            _assert_terminal_transaction(
                malformed,
                pay10_event="pay10:record_unknown",
                outcome="unknown",
            )

        stage0, result, error = await _execute("stage0")
        assert result is None
        assert isinstance(error, DBAPIError)
        assert stage0.provider_calls == 0
        assert "refund-0:commit" in stage0.events
        assert "refund-1:rollback" in stage0.events
        assert "pay24:start_refund_admission" not in stage0.events
        assert "provider:submit_refund" not in stage0.events
        assert not any(
            event.startswith("pay10:record_") for event in stage0.events
        )

        start_denied, result, error = await _execute("start_denied")
        assert result is None
        assert isinstance(error, DBAPIError)
        assert start_denied.provider_calls == 0
        assert "refund-1:commit" in start_denied.events
        assert "refund-2:rollback" in start_denied.events
        assert "provider:submit_refund" not in start_denied.events
        assert not any(
            event.startswith("pay10:record_")
            for event in start_denied.events
        )

        for mode, state in (
            ("start_expired", "expired"),
            ("start_revoked", "revoked"),
        ):
            no_effect, result, error = await _execute(mode)
            assert error is None
            assert result is not None
            assert result.state in {"retry_pending", "dead_lettered"}
            assert result.provider_called is False
            assert no_effect.provider_calls == 0
            _assert_non_active_start_committed_after_inspection(
                no_effect,
                state=state,
            )
            _assert_pay10_only_terminal_transaction(
                no_effect,
                pay10_event="pay10:record_failure:retryable",
            )

        for mode, state in (
            ("start_unknown", "unknown"),
            ("start_completed", "completed"),
        ):
            inconsistent, result, error = await _execute(mode)
            assert error is None
            assert result is not None
            assert result.state == "reconciliation_pending"
            assert result.provider_called is False
            assert inconsistent.provider_calls == 0
            _assert_non_active_start_committed_after_inspection(
                inconsistent,
                state=state,
            )
            _assert_pay10_only_terminal_transaction(
                inconsistent,
                pay10_event="pay10:record_unknown",
            )

        unexpected, result, error = await _execute("start_unexpected")
        assert result is None
        assert isinstance(error, FinanceProviderConfigError)
        assert unexpected.provider_calls == 0
        assert "pay24:inspect_started_state:admitted" in unexpected.events
        assert "refund-2:rollback" in unexpected.events
        assert "refund-2:commit" not in unexpected.events
        assert "provider:submit_refund" not in unexpected.events

        programming_error, result, error = await _execute(
            "programming_error"
        )
        assert result is None
        assert isinstance(error, RuntimeError)
        assert str(error) == "Injected unrelated P3 programming error."
        assert programming_error.provider_calls == 1
        assert programming_error.events.count("provider:submit_refund") == 1
        assert "pay10:validate_outcome" in programming_error.events
        assert "refund-3:rollback" in programming_error.events
        assert "pay10:record_unknown" not in programming_error.events
        assert not any(
            event.startswith("pay24:finish_refund_admission")
            for event in programming_error.events
        )

        replay_expectations = {
            "replay_active": (
                "reconciliation_pending",
                "pay10:record_unknown",
                True,
            ),
            "replay_unknown": (
                "reconciliation_pending",
                "pay10:record_unknown",
                True,
            ),
            "replay_completed": (
                "reconciliation_pending",
                "pay10:record_unknown",
                False,
            ),
            "replay_expired": (
                "retry_pending",
                "pay10:record_failure:retryable",
                False,
            ),
            "replay_revoked": (
                "retry_pending",
                "pay10:record_failure:retryable",
                False,
            ),
        }
        for mode, (expected_state, terminal_event, finishes_admission) in (
            replay_expectations.items()
        ):
            replay, result, error = await _execute(mode)
            assert error is None
            assert result is not None
            assert result.state == expected_state
            assert result.provider_called is False
            assert replay.provider_calls == 0
            assert terminal_event in replay.events
            assert "pay24:start_refund_admission" not in replay.events
            assert "provider:submit_refund" not in replay.events
            assert any(
                event.startswith("pay24:finish_refund_admission")
                for event in replay.events
            ) is finishes_admission
            if finishes_admission:
                _assert_terminal_transaction(
                    replay,
                    pay10_event="pay10:record_unknown",
                    outcome="unknown",
                )

        retry_command_id = uuid.uuid4()
        reclaimed, result, error = await _execute(
            "replay_active",
            attempt_count=1,
            reclaimed_existing_attempt=True,
            command_id=retry_command_id,
        )
        assert error is None
        assert result is not None
        assert result.reclaimed_existing_attempt is True
        assert result.provider_called is False
        assert reclaimed.provider_calls == 0
        assert reclaimed.requested_logical_operation_ids == [
            f"refund:{retry_command_id}:1"
        ]

        new_attempt, result, error = await _execute(
            "success",
            attempt_count=2,
            reclaimed_existing_attempt=False,
            command_id=retry_command_id,
        )
        assert error is None
        assert result is not None
        assert result.provider_called is True
        assert new_attempt.provider_calls == 1
        assert new_attempt.claim.attempt_count == 2
        assert new_attempt.requested_logical_operation_ids == [
            f"refund:{retry_command_id}:2"
        ]
        _assert_claim_and_p1(new_attempt)
        _assert_provider_fences(new_attempt)

        assert not hasattr(
            refund_worker,
            "FinanceRefundFinancialFinalizationService",
        )
        assert all(
            "finaliz" not in event.lower() for event in success.events
        )
    finally:
        refund_worker.FinanceRefundProviderExecutionService = original_service


def test_pay24b_refund_provider_boundary_runtime_composition() -> None:
    asyncio.run(_certify())
