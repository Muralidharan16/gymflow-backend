from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

import app.routers.member_subscriptions_v2 as member_api
from app.core.deps import Staff
from app.finance_core.domain.provider_boundary import (
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig


class _Session:
    def __init__(self, name: str, events: list[str]):
        self.name = name
        self.events = events
        self.info: dict[object, object] = {}
        self.in_tx = False

    def touch(self) -> None:
        self.in_tx = True

    def in_transaction(self) -> bool:
        return self.in_tx

    async def commit(self) -> None:
        self.events.append(f"{self.name}:commit")
        self.in_tx = False

    async def rollback(self) -> None:
        self.events.append(f"{self.name}:rollback")
        self.in_tx = False


class _NeverNetworkTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def post_json(self, **kwargs):
        self.calls += 1
        raise AssertionError("B1B composition test attempted provider network I/O")

    async def get_json(self, **kwargs):
        self.calls += 1
        raise AssertionError("B1B composition test attempted provider network I/O")


class _Scenario:
    def __init__(self, mode: str):
        self.mode = mode
        self.events: list[str] = []
        self.provider_calls = 0
        self.app_db = _Session("app", self.events)
        self.payment_db = _Session("payment", self.events)
        self.transport = _NeverNetworkTransport()
        self.organization_id = uuid.uuid4()
        self.staff_id = uuid.uuid4()
        self.subscription_id = uuid.uuid4()
        self.invoice_id = uuid.uuid4()
        self.intent_id = uuid.uuid4()
        self.operation_id = uuid.uuid4()
        self.admission_id = uuid.uuid4()
        self.execution_id: uuid.UUID | None = None
        self.payment_context: dict[str, str] | None = None


_CURRENT: _Scenario | None = None


def _provider_error(failure_class: str) -> FinanceProviderOperationError:
    return FinanceProviderOperationError(
        provider_code="razorpay_sandbox",
        operation="create_checkout",
        code=f"B1B_{failure_class.upper()}",
        failure_class=failure_class,
        message=f"Injected {failure_class} provider result.",
    )


class _PaymentEffects:
    def __init__(self, scenario: _Scenario, session: _Session):
        assert session is scenario.payment_db
        assert session is not scenario.app_db
        self.s = scenario
        self.session = session

    async def claim_provider_operation(
        self,
        *,
        prepared,
        lease_owner: uuid.UUID,
    ):
        assert self.session is self.s.payment_db
        assert prepared.provider_operation.operation_id == self.s.operation_id
        self.s.execution_id = lease_owner
        self.session.touch()
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
        *,
        provider_adapter,
        claim,
        lease_owner: uuid.UUID,
        response: ProviderCheckoutIntentResponse,
    ) -> str:
        assert self.session is self.s.payment_db
        assert claim.operation_id == self.s.operation_id
        assert lease_owner == self.s.execution_id
        self.s.events.append("finish_success_validate:payment")

        if response.provider_code != provider_adapter.provider_code:
            raise FinanceProviderOperationError(
                provider_code=provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_CODE_MISMATCH",
                failure_class="unknown",
                message="Provider response identity is inconsistent.",
            )
        if not response.provider_order_ref:
            raise FinanceProviderOperationError(
                provider_code=provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_ORDER_REQUIRED",
                failure_class="unknown",
                message="Provider response did not contain an order reference.",
            )

        self.session.touch()
        self.s.events.append("finish_success:payment")
        return response.provider_order_ref

    async def finish_provider_error(
        self,
        *,
        claim,
        lease_owner: uuid.UUID,
        error: FinanceProviderOperationError,
    ) -> None:
        assert self.session is self.s.payment_db
        assert claim.operation_id == self.s.operation_id
        assert lease_owner == self.s.execution_id
        self.session.touch()
        self.s.events.append(f"finish_error:{error.failure_class}:payment")


class _CheckoutService:
    def __init__(self, scenario: _Scenario, session: _Session):
        assert session is scenario.app_db
        assert session is not scenario.payment_db
        self.s = scenario
        self.session = session

    async def prepare_local_checkout(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
        provider_code: str,
        provider_environment: str,
    ):
        assert self.session is self.s.app_db
        assert organization_id == self.s.organization_id
        assert subscription_id == self.s.subscription_id
        assert provider_code == "razorpay_sandbox"
        assert provider_environment == "test"
        self.session.touch()
        self.s.events.append("prepare_local:app")
        request = ProviderCheckoutIntentRequest(
            invoice_id=self.s.invoice_id,
            amount=Decimal("1180.00"),
            currency_code="INR",
            idempotency_key=(
                f"member-subscription-checkout:"
                f"{self.s.subscription_id}:provider_order"
            ),
        )
        return SimpleNamespace(
            organization_id=self.s.organization_id,
            subscription_id=self.s.subscription_id,
            finance_invoice_id=self.s.invoice_id,
            finance_checkout_intent_id=self.s.intent_id,
            amount=Decimal("1180.00"),
            currency_code="INR",
            provider_order_ref=None,
            provider_request=request,
            provider_request_sha256="a" * 64,
            provider_operation=SimpleNamespace(
                operation_id=self.s.operation_id,
                status="reserved",
            ),
            replayed=False,
        )

    def bind_provider_effects(self, session):
        assert self.session is self.s.app_db
        assert session is self.s.payment_db
        assert session is not self.session
        self.s.events.append("bind:payment")
        return _PaymentEffects(self.s, session)

    def provider_admission_binding(self, *, prepared, claim):
        assert prepared.provider_operation.operation_id == claim.operation_id
        assert claim.operation_id == self.s.operation_id
        return SimpleNamespace(
            logical_operation_id=(
                f"member-subscription-checkout:"
                f"{claim.operation_id}:{claim.lease_fence}"
            ),
            operation_sha=prepared.provider_request_sha256,
        )

    async def call_provider(self, *, prepared, provider_adapter):
        assert self.session is self.s.app_db
        assert self.s.app_db.in_tx is False
        assert self.s.payment_db.in_tx is False
        assert prepared.provider_operation.operation_id == self.s.operation_id
        assert provider_adapter.provider_code == "razorpay_sandbox"

        self.s.provider_calls += 1
        self.s.events.append("provider_call")

        if self.s.mode in {"retryable", "final", "unknown"}:
            raise _provider_error(self.s.mode)
        if self.s.mode == "provider_mismatch":
            return ProviderCheckoutIntentResponse(
                provider_code="different_provider",
                provider_order_ref="order_fake_1",
                status="created",
            )
        if self.s.mode == "missing_order":
            return ProviderCheckoutIntentResponse(
                provider_code="razorpay_sandbox",
                provider_order_ref=None,
                status="created",
            )
        return ProviderCheckoutIntentResponse(
            provider_code="razorpay_sandbox",
            provider_order_ref="order_fake_1",
            status="created",
        )

    def build_result(
        self,
        *,
        prepared,
        provider_adapter,
        provider_order_ref: str,
    ):
        self.s.events.append("build_result")
        return SimpleNamespace(
            finance_invoice_id=prepared.finance_invoice_id,
            finance_checkout_intent_id=prepared.finance_checkout_intent_id,
            checkout_fields=provider_adapter.build_checkout_fields(
                provider_order_ref=provider_order_ref
            ),
            display_amount=prepared.amount,
            display_currency=prepared.currency_code,
        )

    def operation_state_error(self, *, provider_code, status_value):
        raise AssertionError(
            f"unexpected operation state path: {provider_code}/{status_value}"
        )


class _Authority:
    def __init__(self, session):
        assert _CURRENT is not None
        assert session is _CURRENT.payment_db
        assert session is not _CURRENT.app_db
        self.s = _CURRENT
        self.session = session

    async def request_current_provider_admission(
        self,
        *,
        capability,
        logical_operation_id: str,
        operation_sha: str,
        lease_seconds: int,
    ):
        assert self.session is self.s.payment_db
        assert str(capability.value) == "checkout"
        assert logical_operation_id == (
            f"member-subscription-checkout:"
            f"{self.s.operation_id}:7"
        )
        assert operation_sha == "a" * 64
        assert lease_seconds > 0
        self.session.touch()
        self.s.events.append("admission_request:payment")

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
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
    ):
        assert admission_id == self.s.admission_id
        assert execution_id == self.s.execution_id
        self.session.touch()
        self.s.events.append("admission_start:payment")

        if self.s.mode == "start_denied":
            raise DBAPIError(
                "PAY24 admission start",
                {},
                RuntimeError("Admission start denied"),
            )
        if self.s.mode == "start_not_active":
            return SimpleNamespace(state="admitted")
        return SimpleNamespace(state="active")

    async def finish_provider_admission(
        self,
        *,
        admission_id: uuid.UUID,
        execution_id: uuid.UUID,
        outcome: str,
    ):
        assert admission_id == self.s.admission_id
        assert execution_id == self.s.execution_id
        self.session.touch()
        self.s.events.append(f"admission_finish:{outcome}:payment")


def _service_factory(session):
    assert _CURRENT is not None
    return _CheckoutService(_CURRENT, session)


async def _execute(mode: str, *, cross_tenant_path: bool = False):
    global _CURRENT

    scenario = _Scenario(mode)
    _CURRENT = scenario

    staff = Staff(
        id=scenario.staff_id,
        org_id=scenario.organization_id,
        gym_id=None,
        role="owner",
        branch_ids=[],
    )
    config = RazorpaySandboxConfig(
        mode="test",
        key_id="rzp_test_b1b_composition",
        key_secret="b1b-composition-only-key",
        webhook_secret="b1b-composition-only-webhook",
        merchant_reference="b1b-composition",
    )

    app = FastAPI()
    app.include_router(member_api.router)

    async def authenticated_app(scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {}).update(
                {
                    "staff_id": str(scenario.staff_id),
                    "principal_type": "owner",
                    "org_id": str(scenario.organization_id),
                    "gym_id": None,
                    "role": "owner",
                    "branch_ids": [],
                }
            )
        await app(scope, receive, send)

    async def app_db_dependency():
        yield scenario.app_db

    async def payment_db_dependency(request: Request):
        assert request.state.staff_id == str(scenario.staff_id)
        assert request.state.principal_type == "owner"
        assert request.state.org_id == str(scenario.organization_id)
        assert request.state.role == "owner"
        scenario.payment_context = {
            "principal_id": request.state.staff_id,
            "principal_type": request.state.principal_type,
            "org_id": request.state.org_id,
            "role": request.state.role,
        }
        yield scenario.payment_db

    async def staff_dependency():
        return staff

    async def sandbox_dependency():
        return None

    async def config_dependency():
        return config

    async def transport_dependency():
        return scenario.transport

    app.dependency_overrides[member_api.get_db] = app_db_dependency
    app.dependency_overrides[
        member_api.get_finance_payment_db
    ] = payment_db_dependency
    app.dependency_overrides[
        member_api.require_org_admin
    ] = staff_dependency
    app.dependency_overrides[
        member_api.require_finance_checkout_sandbox_enabled
    ] = sandbox_dependency
    app.dependency_overrides[
        member_api.get_razorpay_test_mode_config
    ] = config_dependency
    app.dependency_overrides[
        member_api.get_razorpay_test_mode_transport
    ] = transport_dependency

    path_org = (
        uuid.uuid4() if cross_tenant_path else scenario.organization_id
    )
    transport = ASGITransport(app=authenticated_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://pay24-b1b.test",
    ) as client:
        response = await client.post(
            (
                f"/organizations/{path_org}/member-subscriptions/"
                f"{scenario.subscription_id}/checkout-session"
            )
        )

    return scenario, response


def _assert_payment_terminalization(
    scenario: _Scenario,
    *,
    failure_class: str,
    admission_outcome: str,
) -> None:
    finish_operation = scenario.events.index(
        f"finish_error:{failure_class}:payment"
    )
    finish_admission = scenario.events.index(
        f"admission_finish:{admission_outcome}:payment"
    )
    assert finish_operation < finish_admission
    assert scenario.events[finish_admission + 1] == "payment:commit"
    assert scenario.events.count("payment:commit") == 3


async def _certify() -> None:
    original_service = member_api.SourceBoundMemberSubscriptionCheckoutService
    original_authority = member_api.DurableActivationAuthority

    member_api.SourceBoundMemberSubscriptionCheckoutService = _service_factory
    member_api.DurableActivationAuthority = _Authority

    try:
        success, response = await _execute("success")

        assert response.status_code == 200, response.text
        assert success.provider_calls == 1
        assert success.transport.calls == 0
        assert success.app_db is not success.payment_db
        assert success.events == [
            "prepare_local:app",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "admission_request:payment",
            "payment:commit",
            "admission_start:payment",
            "payment:commit",
            "provider_call",
            "finish_success_validate:payment",
            "finish_success:payment",
            "admission_finish:completed:payment",
            "payment:commit",
            "build_result",
        ]
        assert success.app_db.in_tx is False
        assert success.payment_db.in_tx is False
        assert success.events.count("app:commit") == 1
        assert success.payment_context == {
            "principal_id": str(success.staff_id),
            "principal_type": "owner",
            "org_id": str(success.organization_id),
            "role": "owner",
        }
        app_contexts = [
            value
            for value in success.app_db.info.values()
            if isinstance(value, dict)
        ]
        assert any(
            context.get("principal_id") == str(success.staff_id)
            and context.get("org_id") == str(success.organization_id)
            and context.get("principal_type") == "owner"
            for context in app_contexts
        )

        stage0, response = await _execute("stage0")

        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == (
            "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED"
        )
        assert stage0.provider_calls == 0
        assert stage0.transport.calls == 0
        assert stage0.events == [
            "prepare_local:app",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "admission_request:payment",
            "payment:rollback",
        ]

        replay, response = await _execute("replay")

        assert response.status_code == 200, response.text
        assert replay.provider_calls == 0
        assert replay.transport.calls == 0
        assert replay.events == [
            "prepare_local:app",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "payment:commit",
            "build_result",
        ]
        assert "admission_request:payment" not in replay.events
        assert "admission_start:payment" not in replay.events

        for mode, status_code, detail_code, admission_outcome in (
            (
                "retryable",
                503,
                "PROVIDER_RETRYABLE_FAILURE",
                "completed",
            ),
            (
                "final",
                409,
                "PROVIDER_FINAL_FAILURE",
                "completed",
            ),
            (
                "unknown",
                409,
                "PROVIDER_OUTCOME_UNKNOWN",
                "unknown",
            ),
        ):
            failed, response = await _execute(mode)

            assert response.status_code == status_code, response.text
            assert response.json()["detail"]["code"] == detail_code
            assert failed.provider_calls == 1
            assert failed.transport.calls == 0
            _assert_payment_terminalization(
                failed,
                failure_class=mode,
                admission_outcome=admission_outcome,
            )

        for mode in ("provider_mismatch", "missing_order"):
            malformed, response = await _execute(mode)

            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == (
                "PROVIDER_OUTCOME_UNKNOWN"
            )
            assert malformed.provider_calls == 1
            assert malformed.transport.calls == 0
            assert "finish_success_validate:payment" in malformed.events
            assert "finish_success:payment" not in malformed.events
            _assert_payment_terminalization(
                malformed,
                failure_class="unknown",
                admission_outcome="unknown",
            )

        start_denied, response = await _execute("start_denied")

        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == (
            "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED"
        )
        assert start_denied.provider_calls == 0
        assert start_denied.transport.calls == 0
        assert start_denied.events == [
            "prepare_local:app",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "admission_request:payment",
            "payment:commit",
            "admission_start:payment",
            "payment:rollback",
            "finish_error:retryable:payment",
            "payment:commit",
        ]

        not_active, response = await _execute("start_not_active")

        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == (
            "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED"
        )
        assert not_active.provider_calls == 0
        assert not_active.transport.calls == 0
        assert not_active.events == [
            "prepare_local:app",
            "app:commit",
            "bind:payment",
            "claim:payment",
            "admission_request:payment",
            "payment:commit",
            "admission_start:payment",
            "payment:commit",
            "finish_error:retryable:payment",
            "payment:commit",
        ]

        cross_tenant, response = await _execute(
            "success",
            cross_tenant_path=True,
        )

        assert response.status_code == 403, response.text
        assert cross_tenant.provider_calls == 0
        assert cross_tenant.transport.calls == 0
        assert cross_tenant.events == []
        assert cross_tenant.payment_context == {
            "principal_id": str(cross_tenant.staff_id),
            "principal_type": "owner",
            "org_id": str(cross_tenant.organization_id),
            "role": "owner",
        }
    finally:
        member_api.SourceBoundMemberSubscriptionCheckoutService = original_service
        member_api.DurableActivationAuthority = original_authority


def test_pay24b_b1b_member_checkout_runtime_composition() -> None:
    asyncio.run(_certify())
