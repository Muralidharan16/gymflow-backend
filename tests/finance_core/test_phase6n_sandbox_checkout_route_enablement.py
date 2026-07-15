from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import Depends
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.security import create_access_token
from app.finance_core.api.guards import FinanceCheckoutRoutePosture, get_finance_checkout_route_posture
from app.finance_core.api.payment_boundary import get_checkout_orchestration_service
from app.finance_core.api.schemas import FinanceCheckoutCreateRequest
from app.finance_core.services.checkout_orchestration import FinanceCheckoutOrchestrationService
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter
from app.main import app
from tests.finance_core.test_phase5c_invoice_engine import BILLING_PARTY_ID, ORG_ID, fetch_one, fetch_scalar, seed_master_data
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6c_checkout_orchestration import FakePlanResolver, FakeRazorpayClient
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts



async def seed_route_organization() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, tier, is_active, max_branches, default_currency_code)
                VALUES (:org_id, 'Vitara Test Finance Org', 'vitara-test-finance-org', 'basic'::orgtier, true, 10, 'INR')
                ON CONFLICT (id) DO UPDATE SET is_active = EXCLUDED.is_active
                """
            ),
            {"org_id": ORG_ID},
        )
        await session.commit()


def auth_headers(*, role: str = "admin") -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(ORG_ID), f"{role}@phase6n.test.local", role=role)
    return {"Authorization": f"Bearer {token}"}


def checkout_payload() -> dict[str, str]:
    return {
        "plan_code": "DOERS_PRO_MONTHLY",
        "billing_interval": "monthly",
        "billing_party_id": str(BILLING_PARTY_ID),
    }


def sandbox_posture(**overrides) -> FinanceCheckoutRoutePosture:
    values = {
        "sandbox_checkout_enabled": True,
        "provider_mode": "sandbox",
    }
    values.update(overrides)
    return FinanceCheckoutRoutePosture(**values)


def override_checkout_dependencies(client: FakeRazorpayClient, posture: FinanceCheckoutRoutePosture):
    async def service_override(db: AsyncSession = Depends(get_db)) -> FinanceCheckoutOrchestrationService:
        return FinanceCheckoutOrchestrationService(
            db,
            plan_resolver=FakePlanResolver(),
            razorpay_adapter=RazorpaySandboxAdapter(
                config=sandbox_config(),
                client=client,
            ),
        )

    app.dependency_overrides[get_finance_checkout_route_posture] = lambda: posture
    app.dependency_overrides[get_checkout_orchestration_service] = service_override


def clear_checkout_dependency_overrides() -> None:
    app.dependency_overrides.pop(get_finance_checkout_route_posture, None)
    app.dependency_overrides.pop(get_checkout_orchestration_service, None)


@pytest.mark.asyncio
async def test_checkout_route_remains_disabled_by_default_and_creates_no_finance_rows(client):
    await seed_master_data()
    await seed_route_organization()
    before = await finance_counts()

    response = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers={**auth_headers(), "X-Idempotency-Key": "phase6n-default-disabled"},
        json=checkout_payload(),
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_sandbox_enabled_checkout_route_issues_invoice_creates_intent_and_safe_order(client):
    await seed_master_data()
    await seed_route_organization()
    fake_client = FakeRazorpayClient()
    override_checkout_dependencies(fake_client, sandbox_posture())
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6n-sandbox-checkout"},
            json=checkout_payload(),
        )
    finally:
        clear_checkout_dependency_overrides()

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["display_amount"])) == Decimal("1180.00")
    assert body["display_currency"] == "INR"
    assert body["checkout_fields"] == {"key": "rzp_test_key_id", "order_id": "order_test_1"}
    assert "provider_order_id" not in body
    rendered_body = str(body).lower()
    assert "secret" not in rendered_body
    assert "rzp_test_key_secret" not in rendered_body
    assert "provider_order_ref" not in rendered_body
    assert len(fake_client.requests) == 1

    invoice = await fetch_one(
        """
        SELECT status, official_invoice_number, grand_total_amount
        FROM finance.invoices
        WHERE id = :invoice_id
        """,
        {"invoice_id": uuid.UUID(body["finance_invoice_id"])},
    )
    assert invoice["status"] == "issued"
    assert invoice["official_invoice_number"] is not None
    assert invoice["grand_total_amount"] == Decimal("1180.00")

    payment = await fetch_one(
        """
        SELECT status, provider_code, provider_order_ref, provider_payment_ref, amount, currency_code
        FROM finance.payments
        WHERE id = :payment_id
        """,
        {"payment_id": uuid.UUID(body["finance_checkout_intent_id"])},
    )
    assert payment["status"] == "created"
    assert payment["provider_code"] == "razorpay_sandbox"
    assert payment["provider_order_ref"] == "order_test_1"
    assert payment["provider_payment_ref"] is None
    assert payment["amount"] == Decimal("1180.00")
    assert payment["currency_code"] == "INR"


@pytest.mark.asyncio
async def test_sandbox_checkout_replay_does_not_duplicate_invoice_intent_or_provider_order(client):
    await seed_master_data()
    await seed_route_organization()
    fake_client = FakeRazorpayClient()
    override_checkout_dependencies(fake_client, sandbox_posture())
    try:
        first = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6n-route-replay"},
            json=checkout_payload(),
        )
        replay = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6n-route-replay"},
            json=checkout_payload(),
        )
    finally:
        clear_checkout_dependency_overrides()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["finance_invoice_id"] == first.json()["finance_invoice_id"]
    assert replay.json()["finance_checkout_intent_id"] == first.json()["finance_checkout_intent_id"]
    assert len(fake_client.requests) == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.payments") == 1


@pytest.mark.asyncio
async def test_sandbox_checkout_rejects_unsafe_postures_before_service_or_finance_mutation(client):
    unsafe_postures = [
        sandbox_posture(provider_mode="live"),
        sandbox_posture(live_provider_enabled=True),
        sandbox_posture(live_money_movement_enabled=True),
        sandbox_posture(customer_facing_checkout_enabled=True),
        sandbox_posture(public_webhook_enabled=True),
        sandbox_posture(internal_payment_application_enabled=True),
    ]

    for index, posture in enumerate(unsafe_postures):
        await seed_master_data()
        await seed_route_organization()
        fake_client = FakeRazorpayClient()
        override_checkout_dependencies(fake_client, posture)
        before = await finance_counts()
        try:
            response = await client.post(
                "/api/v1/finance/payments/checkout-sessions",
                headers={**auth_headers(), "X-Idempotency-Key": f"phase6n-unsafe-{index}"},
                json=checkout_payload(),
            )
        finally:
            clear_checkout_dependency_overrides()

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "FINANCE_CHECKOUT_SANDBOX_POSTURE_UNSAFE"
        assert "secret" not in str(response.json()).lower()
        assert fake_client.requests == []
        assert await finance_counts() == before


@pytest.mark.asyncio
async def test_sandbox_checkout_auth_and_dto_contract_reject_customer_or_tenant_override(client):
    await seed_master_data()
    await seed_route_organization()
    fake_client = FakeRazorpayClient()
    override_checkout_dependencies(fake_client, sandbox_posture())
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(role="member"), "X-Idempotency-Key": "phase6n-member-denied"},
            json=checkout_payload(),
        )
    finally:
        clear_checkout_dependency_overrides()

    assert response.status_code == 403
    assert fake_client.requests == []

    for field in ("organization_id", "facility_id", "amount", "currency_code", "tax_amount", "provider_order_ref", "activate_subscription", "entitlement_projection"):
        with pytest.raises(ValidationError):
            FinanceCheckoutCreateRequest.model_validate({**checkout_payload(), field: str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_sandbox_checkout_has_no_payment_allocation_ledger_paid_invoice_or_subscription_side_effects(client):
    await seed_master_data()
    await seed_route_organization()
    fake_client = FakeRazorpayClient()
    override_checkout_dependencies(fake_client, sandbox_posture())
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6n-no-side-effects"},
            json=checkout_payload(),
        )
    finally:
        clear_checkout_dependency_overrides()

    assert response.status_code == 200
    assert await fetch_scalar("SELECT count(*) FROM finance.payments WHERE status = 'captured'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


@pytest.mark.asyncio
async def test_sandbox_checkout_enablement_does_not_enable_status_webhook_internal_apply_or_admin_routes(client):
    await seed_master_data()
    await seed_route_organization()
    fake_client = FakeRazorpayClient()
    override_checkout_dependencies(fake_client, sandbox_posture())
    before = await finance_counts()
    try:
        status_response = await client.get(
            f"/api/v1/finance/payments/checkout-sessions/{uuid.uuid4()}",
            headers=auth_headers(),
        )
        webhook_response = await client.post(
            "/api/v1/finance/payments/webhooks/razorpay",
            content=b'{"event":"payment.captured"}',
            headers={"X-Razorpay-Signature": "signed-fixture"},
        )
        apply_response = await client.post(
            "/api/v1/finance/payments/internal/payment-applications",
            headers={**auth_headers(), "X-Finance-Internal-Actor": "finance_core"},
            json={
                "payment_id": str(uuid.uuid4()),
                "invoice_id": str(uuid.uuid4()),
                "amount": "1180.00",
                "currency_code": "INR",
                "idempotency_key": "phase6n-apply-disabled",
                "internal_actor": "finance_core",
                "reason": "sandbox checkout must not enable apply",
            },
        )
        admin_response = await client.get(
            f"/api/v1/finance/payments/admin/payments/{uuid.uuid4()}",
            headers=auth_headers(role="finance_admin"),
        )
    finally:
        clear_checkout_dependency_overrides()

    assert_disabled(status_response)
    assert_disabled(webhook_response)
    assert_disabled(apply_response)
    assert_disabled(admin_response)
    assert fake_client.requests == []
    assert await finance_counts() == before
