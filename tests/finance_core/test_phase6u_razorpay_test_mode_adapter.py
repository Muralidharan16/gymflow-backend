from __future__ import annotations

import base64
import uuid
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.finance_core.domain.provider_boundary import FinanceProviderConfigError, ProviderCheckoutIntentRequest
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    RazorpayProviderError,
    RazorpaySandboxConfig,
    map_razorpay_order_response,
    validate_razorpay_sandbox_config,
)
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter, RazorpayTestModeOrdersClient
from tests.finance_core.test_phase5c_invoice_engine import fetch_one
from tests.finance_core.test_phase5d_payment_ledger import seed_finance_foundation
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config


class FakeRazorpayTestModeTransport:
    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self.response = response
        self.error = error

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
        if self.error is not None:
            raise self.error
        return self.response or {
            "id": "order_phase6u_test",
            "amount": payload["amount"],
            "currency": payload["currency"],
            "receipt": payload["receipt"],
            "status": "created",
        }


def provider_request(*, amount: Decimal = Decimal("1180.00"), idempotency_key: str = "phase6u-order") -> ProviderCheckoutIntentRequest:
    return ProviderCheckoutIntentRequest(
        invoice_id=uuid.UUID("00000000-0000-0000-0000-0000000006a0"),
        amount=amount,
        currency_code="INR",
        idempotency_key=idempotency_key,
    )


def order_request() -> RazorpayOrderCreateRequest:
    return RazorpayOrderCreateRequest(
        amount_subunits=118000,
        currency_code="INR",
        receipt="fin_000000000000000000000000000006a0",
        notes={
            "finance_invoice_id": "00000000-0000-0000-0000-0000000006a0",
            "finance_idempotency_key": "phase6u-order",
        },
    )


@pytest.mark.parametrize("mode", ["sandbox", "test"])
def test_phase6u_config_accepts_only_test_or_sandbox_mode(mode: str):
    config = sandbox_config(mode=mode)

    validated = validate_razorpay_sandbox_config(config)

    assert validated.mode == mode
    with pytest.raises(FrozenInstanceError):
        validated.key_secret = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "config",
    [
        sandbox_config(mode="live", key_secret="private_test_secret", webhook_secret="private_webhook_secret"),
        sandbox_config(key_id="rzp_live_key", key_secret="private_test_secret", webhook_secret="private_webhook_secret"),
        sandbox_config(key_secret="", webhook_secret="private_webhook_secret"),
        sandbox_config(key_secret="rzp_live_private_secret", webhook_secret="private_webhook_secret"),
        RazorpaySandboxConfig(
            mode="test",
            key_id="rzp_test_key_id",
            key_secret="private_test_secret",
            webhook_secret="private_webhook_secret",
            merchant_reference="vitara_sandbox_merchant",
            api_base_url="https://live.example.invalid/v1",
        ),
        RazorpaySandboxConfig(
            mode="test",
            key_id="rzp_test_key_id",
            key_secret="private_test_secret",
            webhook_secret="private_webhook_secret",
            merchant_reference="vitara_sandbox_merchant",
            timeout_seconds=Decimal("0"),
        ),
    ],
)
def test_phase6u_config_rejects_unsafe_posture_without_leaking_secret_values(config: RazorpaySandboxConfig):
    with pytest.raises(FinanceProviderConfigError) as exc:
        validate_razorpay_sandbox_config(config)

    rendered = str(exc.value)
    assert "private_test_secret" not in rendered
    assert "private_webhook_secret" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.asyncio
async def test_phase6u_test_mode_client_builds_order_request_with_basic_auth_and_safe_payload():
    config = sandbox_config(key_secret="private_test_secret")
    transport = FakeRazorpayTestModeTransport()
    client = RazorpayTestModeOrdersClient(config=config, transport=transport)

    response = await client.create_order(order_request())

    assert response.order_id == "order_phase6u_test"
    assert response.amount_subunits == 118000
    assert response.currency_code == "INR"
    assert response.status == "created"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.razorpay.com/v1/orders"
    assert call["payload"] == {
        "amount": 118000,
        "currency": "INR",
        "receipt": "fin_000000000000000000000000000006a0",
        "notes": {
            "finance_invoice_id": "00000000-0000-0000-0000-0000000006a0",
            "finance_idempotency_key": "phase6u-order",
        },
    }
    token = call["headers"]["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode("utf-8") == "rzp_test_key_id:private_test_secret"
    rendered_payload = str(call["payload"]).lower()
    assert "secret" not in rendered_payload
    assert "email" not in rendered_payload
    assert "phone" not in rendered_payload


def test_phase6u_provider_order_response_maps_to_safe_output_without_raw_payload_or_secret():
    result = map_razorpay_order_response(
        payload={
            "id": "order_phase6u_safe",
            "amount": 118000,
            "currency": "inr",
            "receipt": "fin_000000000000000000000000000006a0",
            "status": "created",
            "notes": {"unsafe_raw": "not returned"},
        },
        expected=order_request(),
        public_key_id="rzp_test_key_id",
    )

    safe = result.to_safe_output()
    assert safe == {
        "provider_order_id": "order_phase6u_safe",
        "amount_subunits": 118000,
        "currency_code": "INR",
        "receipt": "fin_000000000000000000000000000006a0",
        "status": "created",
        "public_key_id": "rzp_test_key_id",
    }
    rendered = str(safe).lower()
    assert "secret" not in rendered
    assert "unsafe_raw" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "pay_not_order", "amount": 118000, "currency": "INR", "receipt": "fin_000000000000000000000000000006a0"},
        {"id": "order_bad_amount", "amount": 1, "currency": "INR", "receipt": "fin_000000000000000000000000000006a0"},
        {"id": "order_bad_currency", "amount": 118000, "currency": "USD", "receipt": "fin_000000000000000000000000000006a0"},
        {"id": "order_bad_receipt", "amount": 118000, "currency": "INR", "receipt": "unsafe"},
    ],
)
def test_phase6u_provider_order_response_mismatches_fail_safely(payload: dict[str, Any]):
    with pytest.raises(RazorpayProviderError) as exc:
        map_razorpay_order_response(payload=payload, expected=order_request(), public_key_id="rzp_test_key_id")

    assert "secret" not in str(exc.value).lower()
    assert "private" not in str(exc.value).lower()


@pytest.mark.asyncio
async def test_phase6u_transport_error_paths_are_sanitized():
    client = RazorpayTestModeOrdersClient(
        config=sandbox_config(key_secret="private_test_secret"),
        transport=FakeRazorpayTestModeTransport(error=TimeoutError("private_test_secret timeout detail")),
    )

    with pytest.raises(RazorpayProviderError) as exc:
        await client.create_order(order_request())

    rendered = str(exc.value)
    assert "RAZORPAY_TIMEOUT" in rendered
    assert "private_test_secret" not in rendered


@pytest.mark.asyncio
async def test_phase6u_adapter_rejects_unsafe_order_notes_before_client_call():
    class ForbiddenClient:
        called = False

        async def create_order(self, request: RazorpayOrderCreateRequest):
            self.called = True
            raise AssertionError("unsafe order metadata must fail before client call")

    forbidden_client = ForbiddenClient()
    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=forbidden_client)

    with pytest.raises(RazorpayProviderError) as exc:
        await adapter.create_checkout_intent(provider_request(idempotency_key="contains-secret-token"))

    assert forbidden_client.called is False
    assert "secret-token" not in str(exc.value)


@pytest.mark.asyncio
async def test_phase6u_order_creation_has_no_payment_capture_allocation_ledger_invoice_paid_or_subscription_side_effects():
    await seed_finance_foundation()
    before = await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payments WHERE status = 'captured') AS captured_payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.invoices WHERE status = 'paid') AS paid_invoices,
            (SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions') AS subscription_tables
        """
    )

    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=RazorpayTestModeOrdersClient(config=sandbox_config(), transport=FakeRazorpayTestModeTransport()))
    await adapter.create_checkout_intent(provider_request())

    after = await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payments WHERE status = 'captured') AS captured_payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.invoices WHERE status = 'paid') AS paid_invoices,
            (SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions') AS subscription_tables
        """
    )
    assert after == before


def test_phase6u_does_not_add_default_network_client_public_enablement_or_frontend():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "require_finance_payment_api_enabled" in combined
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
