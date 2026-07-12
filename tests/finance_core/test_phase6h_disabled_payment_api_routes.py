from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.security import create_access_token
from app.finance_core.api.schemas import FinanceCheckoutCreateRequest
from tests.finance_core.test_phase5c_invoice_engine import fetch_scalar


DISABLED_CODE = "FINANCE_PAYMENT_API_DISABLED"


def auth_headers() -> dict[str, str]:
    token = create_access_token(str(uuid.uuid4()), str(uuid.uuid4()), "finance@test.local", role="admin")
    return {"Authorization": f"Bearer {token}"}



def assert_disabled(response):
    assert response.status_code == 404
    body = response.json()
    assert body["detail"]["code"] == DISABLED_CODE
    assert "secret" not in str(body).lower()
    assert "razorpay" not in str(body).lower()


async def finance_counts():
    return {
        "invoices": await fetch_scalar("SELECT count(*) FROM finance.invoices"),
        "payments": await fetch_scalar("SELECT count(*) FROM finance.payments"),
        "events": await fetch_scalar("SELECT count(*) FROM finance.payment_events"),
        "allocations": await fetch_scalar("SELECT count(*) FROM finance.payment_allocations"),
        "ledger": await fetch_scalar("SELECT count(*) FROM finance.ledger_entries"),
        "paid_invoices": await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'"),
    }


@pytest.mark.asyncio
async def test_disabled_checkout_route_rejects_before_invoice_intent_or_provider_creation(client):
    before = await finance_counts()
    response = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers=auth_headers(),
        json={
            "plan_code": "starter",
            "billing_interval": "monthly",
            "billing_party_id": str(uuid.uuid4()),
        },
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_checkout_dto_rejects_browser_financial_authority_fields():
    forbidden_fields = {
        "amount": "1180.00",
        "currency_code": "INR",
        "tax_rate": "18.00",
        "cgst_amount": "90.00",
        "legal_entity_id": str(uuid.uuid4()),
        "division_id": str(uuid.uuid4()),
        "brand_id": str(uuid.uuid4()),
        "invoice_number": "VS/2425/00001",
        "provider_order_ref": "order_test_1",
        "provider_payment_ref": "pay_test_1",
        "activate_subscription": True,
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
async def test_disabled_checkout_status_route_rejects_safely(client):
    before = await finance_counts()
    response = await client.get(f"/api/v1/finance/payments/checkout-sessions/{uuid.uuid4()}", headers=auth_headers())

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_webhook_route_rejects_before_signature_or_state_mutation(client):
    before = await finance_counts()
    response = await client.post(
        "/api/v1/finance/payments/webhooks/razorpay",
        content=b'{"event":"payment.captured"}',
        headers={**auth_headers(), "X-Razorpay-Signature": "bad"},
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_internal_payment_application_route_rejects_before_allocation_ledger_or_paid_marking(client):
    before = await finance_counts()
    response = await client.post(
        "/api/v1/finance/payments/internal/payment-applications",
        headers=auth_headers(),
        json={
            "payment_id": str(uuid.uuid4()),
            "invoice_id": str(uuid.uuid4()),
            "amount": "1180.00",
            "currency_code": "INR",
            "idempotency_key": "phase6h-application-test",
            "internal_actor": "finance_core",
            "reason": "disabled route test",
        },
    )

    assert_disabled(response)
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_disabled_admin_payment_status_route_rejects_safely(client):
    before = await finance_counts()
    response = await client.get(f"/api/v1/finance/payments/admin/payments/{uuid.uuid4()}", headers=auth_headers())

    assert_disabled(response)
    assert await finance_counts() == before


def test_phase6h_source_has_no_real_provider_network_secret_or_subscription_behavior():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

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
