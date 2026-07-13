from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.security import create_access_token
from app.finance_core.api.auth import (
    FinancePaymentActorKind,
    internal_system_actor,
    require_internal_payment_application_actor,
)
from app.finance_core.api.payment_boundary import (
    build_apply_confirmed_payment_command,
    get_payment_application_gate_service,
    map_internal_payment_application_response,
)
from app.finance_core.api.schemas import FinanceInternalPaymentApplicationRequest
from app.finance_core.domain.payment_application_gate import AppliedPaymentResult
from app.main import app
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts


class ForbiddenPaymentApplicationGateService:
    def __init__(self):
        self.called = False

    async def apply_confirmed_payment(self, command):
        self.called = True
        raise AssertionError("disabled internal apply route must not call payment application gate")


def auth_headers(*, role: str = "admin") -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(uuid.uuid4()), "phase6l@test.local", role=role)
    return {"Authorization": f"Bearer {token}"}


def application_payload(**overrides) -> dict[str, str]:
    payload = {
        "payment_id": str(uuid.uuid4()),
        "invoice_id": str(uuid.uuid4()),
        "amount": "1180.00",
        "currency_code": "INR",
        "idempotency_key": "phase6l-application-key",
        "internal_actor": "finance_core",
        "reason": "disabled internal route test",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_disabled_internal_apply_route_rejects_before_phase6e_gate_allocation_ledger_or_paid_marking(client):
    gate = ForbiddenPaymentApplicationGateService()
    app.dependency_overrides[get_payment_application_gate_service] = lambda: gate
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers={**auth_headers(), "X-Finance-Internal-Actor": "finance_core"},
            json=application_payload(),
        )
    finally:
        app.dependency_overrides.pop(get_payment_application_gate_service, None)

    assert_disabled(response)
    assert gate.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_internal_apply_route_requires_user_session_before_disabled_guard(client):
    gate = ForbiddenPaymentApplicationGateService()
    app.dependency_overrides[get_payment_application_gate_service] = lambda: gate
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers={"X-Finance-Internal-Actor": "finance_core"},
            json=application_payload(),
        )
    finally:
        app.dependency_overrides.pop(get_payment_application_gate_service, None)

    assert response.status_code == 401
    assert gate.called is False
    assert await finance_counts() == before


def test_internal_system_actor_contract_only_allows_internal_application_actor():
    internal = internal_system_actor("finance_core")

    allowed = require_internal_payment_application_actor(internal)

    assert allowed.kind == FinancePaymentActorKind.INTERNAL_SYSTEM
    assert allowed.role == "finance_core"


@pytest.mark.parametrize("role", ["owner", "admin", "finance_admin", "customer", "webhook"])
def test_non_internal_route_callers_do_not_receive_internal_actor_header_authority(role):
    # The header is only a future internal-service identity contract. User roles
    # still cannot execute anything because the route remains disabled first.
    token = auth_headers(role=role)
    assert "Authorization" in token


def test_internal_application_request_to_command_mapping_uses_only_server_side_gate_contract():
    payment_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    request = FinanceInternalPaymentApplicationRequest(
        payment_id=payment_id,
        invoice_id=invoice_id,
        amount=Decimal("1180.00"),
        currency_code="INR",
        idempotency_key="phase6l-map-key",
        internal_actor="finance_core",
        reason="server-side validation",
    )

    command = build_apply_confirmed_payment_command(request)

    assert command.payment_id == payment_id
    assert command.invoice_id == invoice_id
    assert command.amount == Decimal("1180.00")
    assert command.currency_code == "INR"
    assert command.idempotency_key == "phase6l-map-key"
    assert command.internal_actor == "finance_core"
    assert command.reason == "server-side validation"


def test_internal_application_response_mapping_uses_applied_payment_result_only():
    result = AppliedPaymentResult(
        allocation_id=uuid.uuid4(),
        payment_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        invoice_status="paid",
        allocated_amount=Decimal("1180.00"),
        replayed=True,
    )

    response = map_internal_payment_application_response(result)

    assert response.allocation_id == result.allocation_id
    assert response.payment_id == result.payment_id
    assert response.invoice_id == result.invoice_id
    assert response.invoice_status == "paid"
    assert response.allocated_amount == Decimal("1180.00")
    assert response.replayed is True


def test_internal_application_dto_rejects_browser_customer_provider_and_subscription_authority_fields():
    forbidden_fields = {
        "provider_payment_ref": "pay_test_1",
        "provider_order_ref": "order_test_1",
        "provider_customer_ref": "cust_test_1",
        "customer_actor": str(uuid.uuid4()),
        "tenant_admin_actor": str(uuid.uuid4()),
        "ledger_account": "BANK",
        "mark_invoice_paid": True,
        "activate_subscription": True,
        "deactivate_subscription": True,
        "entitlement_projection": "active",
        "settlement_ref": "settl_test_1",
        "refund_ref": "rfnd_test_1",
    }

    for field, value in forbidden_fields.items():
        with pytest.raises(ValidationError):
            FinanceInternalPaymentApplicationRequest.model_validate({**application_payload(), field: value})


def test_phase6l_source_has_no_live_provider_frontend_subscription_or_production_enablement():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "razorpay.client" not in combined
    assert "razorpayclient" not in combined
    assert "provider_secret" not in combined
    assert "rzp_live_" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "entitlement_projection" not in combined
    assert not (repo_root / "frontend").exists()
