from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.security import create_access_token
from app.finance_core.api.auth import (
    FinancePaymentActor,
    FinancePaymentActorKind,
    internal_system_actor,
    require_checkout_actor,
    require_checkout_status_actor,
    require_finance_admin_actor,
    require_internal_payment_application_actor,
    require_same_tenant,
    require_webhook_signature_actor,
)
from app.finance_core.api.guards import FINANCE_PAYMENT_API_ENABLED
from app.finance_core.api.schemas import FinanceCheckoutCreateRequest
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts


def auth_headers(*, role: str = "admin", org_id: uuid.UUID | None = None, branch_ids: list[str] | None = None) -> dict[str, str]:
    token = create_access_token(
        str(uuid.uuid4()),
        str(org_id or uuid.uuid4()),
        f"{role}@finance.test.local",
        role=role,
        branch_ids=branch_ids or [],
    )
    return {"Authorization": f"Bearer {token}"}


def actor(
    kind: FinancePaymentActorKind,
    *,
    organization_id: uuid.UUID | None = None,
    facility_id: uuid.UUID | None = None,
) -> FinancePaymentActor:
    return FinancePaymentActor(
        kind=kind,
        organization_id=organization_id or uuid.uuid4(),
        facility_id=facility_id,
        staff_id=uuid.uuid4(),
        role=kind.value,
    )


@pytest.mark.asyncio
async def test_payment_api_disabled_by_default_even_with_valid_tenant_admin(client):
    assert FINANCE_PAYMENT_API_ENABLED is False
    before = await finance_counts()

    response = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers=auth_headers(role="admin"),
        json={
            "plan_code": "starter",
            "billing_interval": "monthly",
            "billing_party_id": str(uuid.uuid4()),
        },
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_missing_auth_fails_safely_before_disabled_route_behavior(client):
    before = await finance_counts()

    response = await client.get(f"/api/v1/finance/payments/checkout-sessions/{uuid.uuid4()}")

    assert response.status_code == 401
    assert "secret" not in str(response.json()).lower()
    assert await finance_counts() == before


def test_customer_actor_cannot_access_internal_payment_application_or_admin_status():
    customer = actor(FinancePaymentActorKind.CUSTOMER)

    with pytest.raises(HTTPException) as internal_error:
        require_internal_payment_application_actor(customer)
    assert internal_error.value.status_code == 403

    with pytest.raises(HTTPException) as admin_error:
        require_finance_admin_actor(customer)
    assert admin_error.value.status_code == 403


def test_tenant_admin_cannot_access_other_tenant_or_facility_finance_status():
    org_id = uuid.uuid4()
    facility_id = uuid.uuid4()
    tenant_admin = actor(FinancePaymentActorKind.TENANT_ADMIN, organization_id=org_id, facility_id=facility_id)

    require_same_tenant(tenant_admin, organization_id=org_id, facility_id=facility_id)

    with pytest.raises(HTTPException) as other_org_error:
        require_same_tenant(tenant_admin, organization_id=uuid.uuid4(), facility_id=facility_id)
    assert other_org_error.value.status_code == 404

    with pytest.raises(HTTPException) as other_facility_error:
        require_same_tenant(tenant_admin, organization_id=org_id, facility_id=uuid.uuid4())
    assert other_facility_error.value.status_code == 404


def test_tenant_admin_cannot_call_internal_payment_application_route_contract():
    tenant_admin = actor(FinancePaymentActorKind.TENANT_ADMIN)

    with pytest.raises(HTTPException) as exc_info:
        require_internal_payment_application_actor(tenant_admin)

    assert exc_info.value.status_code == 403


def test_internal_system_actor_can_pass_internal_contract_but_not_admin_or_checkout_contract():
    internal = internal_system_actor("finance_core")

    assert require_internal_payment_application_actor(internal) == internal

    with pytest.raises(HTTPException):
        require_checkout_actor(internal)
    with pytest.raises(HTTPException):
        require_finance_admin_actor(internal)


def test_webhook_contract_is_signature_based_not_user_session_based():
    webhook = require_webhook_signature_actor("signed-fixture")
    assert webhook.kind == FinancePaymentActorKind.WEBHOOK
    assert webhook.organization_id is None

    with pytest.raises(HTTPException) as exc_info:
        require_webhook_signature_actor(None)
    assert exc_info.value.status_code == 401


def test_finance_admin_contract_can_inspect_status_but_not_create_checkout_or_apply_payment():
    finance_admin = actor(FinancePaymentActorKind.FINANCE_ADMIN)

    assert require_checkout_status_actor(finance_admin) == finance_admin
    assert require_finance_admin_actor(finance_admin) == finance_admin

    with pytest.raises(HTTPException):
        require_checkout_actor(finance_admin)
    with pytest.raises(HTTPException):
        require_internal_payment_application_actor(finance_admin)


def test_checkout_create_dto_still_rejects_browser_authority_fields():
    forbidden_fields = {
        "amount": "1180.00",
        "currency_code": "INR",
        "tax_rate": "18.00",
        "cgst_amount": "90.00",
        "sgst_amount": "90.00",
        "igst_amount": "180.00",
        "legal_entity_id": str(uuid.uuid4()),
        "division_id": str(uuid.uuid4()),
        "brand_id": str(uuid.uuid4()),
        "invoice_number": "VS/2425/00001",
        "provider_order_ref": "order_test_1",
        "provider_payment_ref": "pay_test_1",
        "provider_customer_ref": "cust_test_1",
        "activate_subscription": True,
        "entitlement_projection": "active",
    }

    for field, value in forbidden_fields.items():
        with pytest.raises(ValidationError):
            FinanceCheckoutCreateRequest.model_validate(
                {
                    "plan_code": "starter",
                    "billing_interval": "monthly",
                    "billing_party_id": uuid.uuid4(),
                    field: value,
                }
            )


@pytest.mark.asyncio
async def test_disabled_checkout_route_still_does_not_create_invoice_intent_or_provider_order(client):
    before = await finance_counts()

    response = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers=auth_headers(role="owner"),
        json={
            "plan_code": "starter",
            "billing_interval": "monthly",
            "billing_party_id": str(uuid.uuid4()),
        },
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_webhook_route_still_does_not_mutate_payment_state(client):
    before = await finance_counts()

    response = await client.post(
        "/api/v1/finance/payments/webhooks/razorpay",
        content=b'{"event":"payment.captured"}',
        headers={"X-Razorpay-Signature": "signed-fixture"},
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_internal_apply_route_still_does_not_allocate_post_ledger_or_mark_invoice_paid(client):
    before = await finance_counts()

    response = await client.post(
        "/api/v1/finance/payments/internal/payment-applications",
        headers={**auth_headers(role="admin"), "X-Finance-Internal-Actor": "finance_core"},
        json={
            "payment_id": str(uuid.uuid4()),
            "invoice_id": str(uuid.uuid4()),
            "amount": "1180.00",
            "currency_code": "INR",
            "idempotency_key": "phase6i-application-test",
            "internal_actor": "finance_core",
            "reason": "disabled route test",
        },
    )

    assert_disabled(response)
    assert await finance_counts() == before
