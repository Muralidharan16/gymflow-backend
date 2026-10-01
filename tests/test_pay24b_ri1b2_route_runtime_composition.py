from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

import app.finance_core.api.payment_boundary as pb
from app.finance_core.api.auth import (
    FinancePaymentActor,
    FinancePaymentActorKind,
)
from app.finance_core.domain.checkout_orchestration import SafeCheckoutSessionResult
from app.finance_core.domain.provider_boundary import FinanceProviderOperationError


class _Session:
    def __init__(self, name: str, events: list[str]):
        self.name = name
        self.events = events
        self.in_tx = False

    def touch(self) -> None:
        self.in_tx = True

    async def commit(self) -> None:
        self.events.append(f"{self.name}:commit")
        self.in_tx = False

    async def rollback(self) -> None:
        self.events.append(f"{self.name}:rollback")
        self.in_tx = False


class _Scenario:
    def __init__(self, mode: str):
        self.mode = mode
        self.events: list[str] = []
        self.provider_calls = 0
        self.app_db = _Session("app", self.events)
        self.payment_db = _Session("payment", self.events)
        self.invoice_id = uuid.uuid4()
        self.intent_id = uuid.uuid4()
        self.operation_id = uuid.uuid4()
        self.admission_id = uuid.uuid4()


_CURRENT: _Scenario | None = None


class _Audit:
    def __init__(self, session):
        assert _CURRENT is not None
        assert session is _CURRENT.app_db
        self.session = session

    async def record(self, **kwargs) -> None:
        assert _CURRENT is not None
        self.session.touch()
        _CURRENT.events.append("audit:app")


class _PaymentEffects:
    def __init__(self, scenario: _Scenario):
        self.s = scenario

    async def claim_provider_operation(self, prepared, *, lease_owner):
        assert self.s.payment_db is not self.s.app_db

        self.s.payment_db.touch()
        self.s.events.append("claim:payment")

        if self.s.mode == "replay":
            return SimpleNamespace(
                claimed=False,
                status="succeeded",
                provider_object_id="order_existing",
                operation_id=self.s.operation_id,
                lease_fence=7,
            )

        return SimpleNamespace(
            claimed=True,
            status="in_flight",
            provider_object_id=None,
            operation_id=self.s.operation_id,
            lease_fence=7,
        )

    async def finish_provider_success(
        self,
        prepared,
        claim,
        *,
        lease_owner,
        response,
    ):
        self.s.payment_db.touch()
        self.s.events.append("finish_success:payment")
        return "order_fake_1"

    async def finish_provider_error(
        self,
        claim,
        *,
        lease_owner,
        error,
    ) -> None:
        self.s.payment_db.touch()
        self.s.events.append(
            f"finish_error:{error.failure_class}:payment"
        )


class _CheckoutService:
    def __init__(self, scenario: _Scenario):
        self.s = scenario

    async def prepare_checkout_session(self, command):
        self.s.app_db.touch()
        self.s.events.append("prepare:app")

        return SimpleNamespace(
            finance_invoice_id=self.s.invoice_id,
            finance_checkout_intent_id=self.s.intent_id,
            amount=Decimal("1180.00"),
            currency_code="INR",
            provider_request=SimpleNamespace(),
            provider_operation=SimpleNamespace(status="reserved"),
            provider_order_id=None,
            replayed=False,
        )

    def bind_provider_effects(self, session):
        assert session is self.s.payment_db
        assert session is not self.s.app_db
        self.s.events.append("bind:payment")
        return _PaymentEffects(self.s)

    def provider_admission_binding(self, prepared, claim):
        return SimpleNamespace(
            logical_operation_id=(
                f"checkout:{claim.operation_id}:{claim.lease_fence}"
            ),
            operation_sha="a" * 64,
        )

    async def call_provider(self, prepared):
        assert self.s.app_db.in_tx is False
        assert self.s.payment_db.in_tx is False

        self.s.provider_calls += 1
        self.s.events.append("provider_call")

        if self.s.mode == "unknown":
            raise FinanceProviderOperationError(
                provider_code="fake",
                operation="create_checkout",
                code="FAKE_PROVIDER_TIMEOUT",
                failure_class="unknown",
                message="Injected ambiguous provider result.",
            )

        return SimpleNamespace(
            provider_order_ref="order_fake_1"
        )

    def build_result(self, prepared, *, provider_order_id):
        self.s.events.append("build_result")

        return SafeCheckoutSessionResult(
            finance_invoice_id=prepared.finance_invoice_id,
            finance_checkout_intent_id=prepared.finance_checkout_intent_id,
            provider_order_id=provider_order_id,
            checkout_fields={
                "key": "rzp_test_public",
                "order_id": provider_order_id,
            },
            display_amount=prepared.amount,
            display_currency=prepared.currency_code,
        )

    def operation_state_error(self, status_value):
        raise AssertionError(
            f"unexpected operation state path: {status_value}"
        )


class _Authority:
    def __init__(self, session):
        assert _CURRENT is not None
        assert session is _CURRENT.payment_db
        assert session is not _CURRENT.app_db
        self.s = _CURRENT

    async def request_current_provider_admission(
        self,
        *,
        capability,
        logical_operation_id,
        operation_sha,
        lease_seconds,
    ):
        self.s.payment_db.touch()
        self.s.events.append(
            "admission_request:payment"
        )

        if self.s.mode == "stage0":
            raise DBAPIError(
                "PAY24 admission",
                {},
                RuntimeError("Stage0 activation denied"),
            )

        return SimpleNamespace(
            admission_id=self.s.admission_id,
            state="admitted",
        )

    async def start_provider_admission(
        self,
        *,
        admission_id,
        execution_id,
    ):
        assert admission_id == self.s.admission_id
        self.s.payment_db.touch()
        self.s.events.append(
            "admission_start:payment"
        )
        return SimpleNamespace(state="active")

    async def finish_provider_admission(
        self,
        *,
        admission_id,
        execution_id,
        outcome,
    ):
        assert admission_id == self.s.admission_id
        self.s.payment_db.touch()
        self.s.events.append(
            f"admission_finish:{outcome}:payment"
        )


def _security_event(*args, **kwargs):
    assert _CURRENT is not None
    _CURRENT.events.append("security_event")


async def _execute(mode: str):
    global _CURRENT

    s = _Scenario(mode)
    _CURRENT = s

    actor = FinancePaymentActor(
        kind=FinancePaymentActorKind.TENANT_ADMIN,
        organization_id=uuid.uuid4(),
        staff_id=uuid.uuid4(),
        role="admin",
    )

    security_context = SimpleNamespace(
        actor_id=actor.staff_id,
        organization_id=actor.organization_id,
    )

    service = _CheckoutService(s)

    app = FastAPI()
    app.include_router(pb.router)

    app.dependency_overrides[
        pb.checkout_actor_dependency
    ] = lambda: actor

    app.dependency_overrides[
        pb.require_finance_checkout_sandbox_enabled
    ] = lambda: None

    app.dependency_overrides[
        pb.finance_high_risk_actor_dependency
    ] = lambda: security_context

    app.dependency_overrides[pb.get_db] = \
        lambda: s.app_db

    app.dependency_overrides[
        pb.get_finance_payment_db
    ] = lambda: s.payment_db

    app.dependency_overrides[
        pb.get_checkout_orchestration_service
    ] = lambda: service

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://pay24.test",
    ) as client:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={
                "X-Idempotency-Key": f"ri1b2-{mode}",
            },
            json={
                "plan_code": "DOERS_PRO_MONTHLY",
                "billing_interval": "monthly",
                "billing_party_id": str(uuid.uuid4()),
            },
        )

    return s, response


async def _certify() -> None:
    original_audit = pb.FinanceSecurityAuditService
    original_authority = pb.DurableActivationAuthority
    original_event = pb.security_event

    pb.FinanceSecurityAuditService = _Audit
    pb.DurableActivationAuthority = _Authority
    pb.security_event = _security_event

    try:
        success, response = await _execute("success")

        assert response.status_code == 200, response.text
        assert success.provider_calls == 1

        assert success.events == [
            "prepare:app",
            "audit:app",
            "security_event",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "admission_request:payment",
            "payment:commit",
            "admission_start:payment",
            "payment:commit",
            "provider_call",
            "finish_success:payment",
            "admission_finish:completed:payment",
            "payment:commit",
            "build_result",
        ]

        assert success.app_db.in_tx is False
        assert success.payment_db.in_tx is False

        stage0, response = await _execute("stage0")

        assert response.status_code == 503, response.text
        assert (
            response.json()["detail"]["code"]
            == "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED"
        )
        assert stage0.provider_calls == 0
        assert "provider_call" not in stage0.events
        assert "payment:rollback" in stage0.events
        assert "payment:commit" not in stage0.events

        replay, response = await _execute("replay")

        assert response.status_code == 200, response.text
        assert replay.provider_calls == 0
        assert (
            "admission_request:payment"
            not in replay.events
        )
        assert "provider_call" not in replay.events
        assert replay.events.count("payment:commit") == 1

        unknown, response = await _execute("unknown")

        assert response.status_code == 409, response.text
        assert (
            response.json()["detail"]["code"]
            == "FINANCE_PROVIDER_OUTCOME_UNKNOWN"
        )
        assert unknown.provider_calls == 1
        assert unknown.events.count("provider_call") == 1

        finish_error = unknown.events.index(
            "finish_error:unknown:payment"
        )
        finish_admission = unknown.events.index(
            "admission_finish:unknown:payment"
        )

        assert finish_error < finish_admission
        assert (
            unknown.events[-1]
            == "payment:commit"
        )
        assert finish_admission < len(unknown.events) - 1

    finally:
        pb.FinanceSecurityAuditService = original_audit
        pb.DurableActivationAuthority = original_authority
        pb.security_event = original_event


def test_pay24b_ri1b2_fastapi_route_runtime_composition():
    asyncio.run(_certify())
