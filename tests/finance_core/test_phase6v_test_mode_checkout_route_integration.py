from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.finance_core.api.guards import FinanceCheckoutRoutePosture, get_finance_checkout_route_posture
from app.finance_core.api.payment_boundary import (
    get_checkout_plan_resolver,
    get_razorpay_test_mode_config,
    get_razorpay_test_mode_transport,
)
from app.finance_core.api.schemas import FinanceCheckoutCreateRequest
from app.main import app
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig
from tests.finance_core.test_phase5c_invoice_engine import BILLING_PARTY_ID, fetch_one, fetch_scalar, seed_master_data
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6c_checkout_orchestration import FakePlanResolver
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts
from tests.finance_core.test_phase6n_sandbox_checkout_route_enablement import auth_headers, checkout_payload, seed_route_organization


class FakeRazorpayTestModeTransport:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "id": f"order_phase6v_{len(self.calls)}",
            "amount": payload["amount"],
            "currency": payload["currency"],
            "receipt": payload["receipt"],
            "status": "created",
            "raw_secret_like_field": "not-returned",
        }


def sandbox_posture(**overrides) -> FinanceCheckoutRoutePosture:
    values = {
        "sandbox_checkout_enabled": True,
        "provider_mode": "test",
    }
    values.update(overrides)
    return FinanceCheckoutRoutePosture(**values)


def override_test_mode_dependencies(*, transport: FakeRazorpayTestModeTransport, config: RazorpaySandboxConfig | None = None) -> None:
    app.dependency_overrides[get_finance_checkout_route_posture] = lambda: sandbox_posture()
    app.dependency_overrides[get_checkout_plan_resolver] = lambda: FakePlanResolver()
    app.dependency_overrides[get_razorpay_test_mode_config] = lambda: config or sandbox_config(
        mode="test",
        key_id="rzp_test_phase6v_key",
        key_secret="private_phase6v_secret",
        webhook_secret="private_phase6v_webhook",
    )
    app.dependency_overrides[get_razorpay_test_mode_transport] = lambda: transport


def clear_test_mode_dependency_overrides() -> None:
    for dependency in (
        get_finance_checkout_route_posture,
        get_checkout_plan_resolver,
        get_razorpay_test_mode_config,
        get_razorpay_test_mode_transport,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.mark.asyncio
async def test_phase6v_default_checkout_runtime_remains_disabled_and_does_not_construct_transport(client):
    await seed_master_data()
    await seed_route_organization()
    transport = FakeRazorpayTestModeTransport()
    app.dependency_overrides[get_razorpay_test_mode_transport] = lambda: transport
    before = await finance_counts()
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-default-disabled"},
            json=checkout_payload(),
        )
    finally:
        clear_test_mode_dependency_overrides()

    assert_disabled(response)
    assert transport.calls == []
    assert await finance_counts() == before


@pytest.mark.asyncio
async def test_phase6v_sandbox_checkout_route_uses_razorpay_test_mode_adapter_with_injected_transport(client):
    await seed_master_data()
    await seed_route_organization()
    transport = FakeRazorpayTestModeTransport()
    override_test_mode_dependencies(transport=transport)
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-test-mode-checkout"},
            json=checkout_payload(),
        )
    finally:
        clear_test_mode_dependency_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_fields"] == {"key": "rzp_test_phase6v_key", "order_id": "order_phase6v_1"}
    assert Decimal(str(body["display_amount"])) == Decimal("1180.00")
    assert body["display_currency"] == "INR"
    rendered_body = str(body).lower()
    assert "private_phase6v_secret" not in rendered_body
    assert "private_phase6v_webhook" not in rendered_body
    assert "raw_secret_like_field" not in rendered_body
    assert "provider_order_ref" not in rendered_body

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.razorpay.com/v1/orders"
    assert call["payload"] == {
        "amount": 118000,
        "currency": "INR",
        "receipt": f"fin_{uuid.UUID(body['finance_invoice_id']).hex[:32]}",
        "notes": {
            "finance_invoice_id": body["finance_invoice_id"],
            "finance_idempotency_key": "phase6v-test-mode-checkout:razorpay_order",
        },
    }
    rendered_payload = str(call["payload"]).lower()
    assert "secret" not in rendered_payload
    assert "email" not in rendered_payload
    assert "phone" not in rendered_payload

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
    assert payment["provider_order_ref"] == "order_phase6v_1"
    assert payment["provider_payment_ref"] is None
    assert payment["amount"] == Decimal("1180.00")
    assert payment["currency_code"] == "INR"


@pytest.mark.asyncio
async def test_phase6v_idempotent_route_replay_does_not_duplicate_invoice_payment_or_provider_order(client):
    await seed_master_data()
    await seed_route_organization()
    transport = FakeRazorpayTestModeTransport()
    override_test_mode_dependencies(transport=transport)
    try:
        first = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-route-replay"},
            json=checkout_payload(),
        )
        replay = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-route-replay"},
            json=checkout_payload(),
        )
    finally:
        clear_test_mode_dependency_overrides()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["finance_invoice_id"] == first.json()["finance_invoice_id"]
    assert replay.json()["finance_checkout_intent_id"] == first.json()["finance_checkout_intent_id"]
    assert replay.json()["checkout_fields"] == first.json()["checkout_fields"]
    assert len(transport.calls) == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices") == 1
    assert await fetch_scalar("SELECT count(*) FROM finance.payments") == 1


@pytest.mark.parametrize(
    "config",
    [
        sandbox_config(mode="live", key_secret="private_phase6v_secret", webhook_secret="private_phase6v_webhook"),
        sandbox_config(key_id="rzp_live_phase6v", key_secret="private_phase6v_secret", webhook_secret="private_phase6v_webhook"),
        sandbox_config(key_secret="", webhook_secret="private_phase6v_webhook"),
        RazorpaySandboxConfig(
            mode="test",
            key_id="rzp_test_phase6v_key",
            key_secret="private_phase6v_secret",
            webhook_secret="private_phase6v_webhook",
            merchant_reference="vitara_phase6v",
            api_base_url="https://unsafe.example.invalid/v1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_phase6v_unsafe_razorpay_test_mode_config_fails_without_transport_call(client, config):
    await seed_master_data()
    await seed_route_organization()
    transport = FakeRazorpayTestModeTransport()
    override_test_mode_dependencies(transport=transport, config=config)
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-unsafe-config"},
            json=checkout_payload(),
        )
    finally:
        clear_test_mode_dependency_overrides()

    assert response.status_code >= 400
    rendered = response.text.lower()
    assert "private_phase6v_secret" not in rendered
    assert "private_phase6v_webhook" not in rendered
    assert transport.calls == []


@pytest.mark.asyncio
async def test_phase6v_checkout_route_dto_rejects_browser_financial_provider_and_activation_authority(client):
    forbidden_fields = (
        "amount",
        "currency_code",
        "tax_amount",
        "gst_split",
        "legal_entity_id",
        "division_id",
        "brand_id",
        "invoice_number",
        "provider_customer_ref",
        "provider_order_ref",
        "provider_payment_ref",
        "mark_invoice_paid",
        "activate_subscription",
        "entitlement_projection",
    )

    for field in forbidden_fields:
        with pytest.raises(ValidationError):
            FinanceCheckoutCreateRequest.model_validate({**checkout_payload(), field: "browser-owned"})


@pytest.mark.asyncio
async def test_phase6v_checkout_order_creation_has_no_capture_allocation_ledger_paid_invoice_or_subscription_side_effects(client):
    await seed_master_data()
    await seed_route_organization()
    transport = FakeRazorpayTestModeTransport()
    override_test_mode_dependencies(transport=transport)
    try:
        response = await client.post(
            "/api/v1/finance/payments/checkout-sessions",
            headers={**auth_headers(), "X-Idempotency-Key": "phase6v-no-side-effects"},
            json=checkout_payload(),
        )
    finally:
        clear_test_mode_dependency_overrides()

    assert response.status_code == 200
    assert await fetch_scalar("SELECT count(*) FROM finance.payments WHERE status = 'captured'") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.payment_allocations") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.ledger_entries") == 0
    assert await fetch_scalar("SELECT count(*) FROM finance.invoices WHERE status = 'paid'") == 0
    assert await fetch_scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions'") >= 0


def test_phase6v_has_no_default_network_client_frontend_or_subscription_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "razorpay.client" not in combined
    assert "razorpayclient" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "rzp_live_" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert not (repo_root / "frontend").exists()
