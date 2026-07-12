from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.security import create_access_token
from app.finance_core.api.payment_boundary import (
    build_checkout_session_command,
    get_checkout_orchestration_service,
    map_checkout_session_response,
)
from app.finance_core.api.schemas import FinanceCheckoutCreateRequest
from app.finance_core.domain.checkout_orchestration import SafeCheckoutSessionResult
from app.finance_core.api.auth import FinancePaymentActor, FinancePaymentActorKind
from app.main import app
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts


class ForbiddenCheckoutOrchestrationService:
    def __init__(self):
        self.called = False

    async def create_checkout_session(self, command):
        self.called = True
        raise AssertionError("disabled checkout route must not call orchestration service")


def auth_headers(*, role: str = "admin") -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(uuid.uuid4()), "phase6j@test.local", role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_disabled_checkout_route_rejects_before_orchestration_invoice_intent_or_provider_order(client):
    service = ForbiddenCheckoutOrchestrationService()
    app.dependency_overrides[get_checkout_orchestration_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6j-disabled-checkout"},
            json={
                "plan_code": "DOERS_PRO_MONTHLY",
                "billing_interval": "monthly",
                "billing_party_id": str(uuid.uuid4()),
            },
        )
    finally:
        app.dependency_overrides.pop(get_checkout_orchestration_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_checkout_route_preserves_safe_404_without_idempotency_header(client):
    service = ForbiddenCheckoutOrchestrationService()
    app.dependency_overrides[get_checkout_orchestration_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers=auth_headers(),
            json={
                "plan_code": "DOERS_PRO_MONTHLY",
                "billing_interval": "monthly",
                "billing_party_id": str(uuid.uuid4()),
            },
        )
    finally:
        app.dependency_overrides.pop(get_checkout_orchestration_service, None)

    assert_disabled(response)
    assert service.called is False
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_missing_auth_still_fails_safely_before_checkout_orchestration(client):
    service = ForbiddenCheckoutOrchestrationService()
    app.dependency_overrides[get_checkout_orchestration_service] = lambda: service
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            json={
                "plan_code": "DOERS_PRO_MONTHLY",
                "billing_interval": "monthly",
                "billing_party_id": str(uuid.uuid4()),
            },
        )
    finally:
        app.dependency_overrides.pop(get_checkout_orchestration_service, None)

    assert response.status_code == 401
    assert service.called is False
    assert await finance_counts() == before


def test_checkout_request_to_command_mapping_uses_only_safe_selectors_and_actor_org():
    org_id = uuid.uuid4()
    billing_party_id = uuid.uuid4()
    actor = FinancePaymentActor(
        kind=FinancePaymentActorKind.TENANT_ADMIN,
        organization_id=org_id,
        staff_id=uuid.uuid4(),
        role="admin",
    )
    request = FinanceCheckoutCreateRequest(
        plan_code="DOERS_PRO_MONTHLY",
        billing_interval="monthly",
        billing_party_id=billing_party_id,
    )

    command = build_checkout_session_command(request, actor=actor, idempotency_key="route-map-key")

    assert command.organization_id == org_id
    assert command.billing_party_id == billing_party_id
    assert command.selector.plan_code == "DOERS_PRO_MONTHLY"
    assert command.selector.billing_interval == "monthly"
    assert command.idempotency_key == "route-map-key"


def test_checkout_command_mapping_rejects_missing_idempotency_key_when_future_route_is_enabled():
    actor = FinancePaymentActor(
        kind=FinancePaymentActorKind.TENANT_ADMIN,
        organization_id=uuid.uuid4(),
        staff_id=uuid.uuid4(),
        role="admin",
    )
    request = FinanceCheckoutCreateRequest(
        plan_code="DOERS_PRO_MONTHLY",
        billing_interval="monthly",
        billing_party_id=uuid.uuid4(),
    )

    with pytest.raises(ValueError):
        build_checkout_session_command(request, actor=actor, idempotency_key=None)


def test_checkout_response_mapping_uses_safe_checkout_session_result_only():
    result = SafeCheckoutSessionResult(
        finance_invoice_id=uuid.uuid4(),
        finance_checkout_intent_id=uuid.uuid4(),
        provider_order_id="order_test_hidden_in_response_model_contract",
        checkout_fields={"key": "rzp_test_public", "order_id": "order_test_1"},
        display_amount=Decimal("1180.00"),
        display_currency="INR",
    )

    response = map_checkout_session_response(result)

    assert response.finance_invoice_id == result.finance_invoice_id
    assert response.finance_checkout_intent_id == result.finance_checkout_intent_id
    assert response.checkout_fields == {"key": "rzp_test_public", "order_id": "order_test_1"}
    assert response.display_amount == Decimal("1180.00")
    assert response.display_currency == "INR"
    assert "provider_order_id" not in response.model_dump()


def test_checkout_dto_rejects_financial_provider_and_activation_authority_fields():
    forbidden_fields = {
        "amount": "1180.00",
        "currency": "INR",
        "currency_code": "INR",
        "tax_amount": "180.00",
        "gst_split": {"cgst": "90.00", "sgst": "90.00"},
        "legal_entity_id": str(uuid.uuid4()),
        "division_id": str(uuid.uuid4()),
        "brand_id": str(uuid.uuid4()),
        "invoice_number": "VS/2425/00001",
        "provider_customer_ref": "cust_test_1",
        "provider_payment_ref": "pay_test_1",
        "provider_order_ref": "order_test_1",
        "activate_subscription": True,
        "entitlement_projection": "active",
    }

    for field, value in forbidden_fields.items():
        with pytest.raises(ValidationError):
            FinanceCheckoutCreateRequest.model_validate(
                {
                    "plan_code": "DOERS_PRO_MONTHLY",
                    "billing_interval": "monthly",
                    "billing_party_id": uuid.uuid4(),
                    field: value,
                }
            )


def test_phase6j_source_has_no_live_provider_frontend_subscription_or_production_enablement():
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
