from __future__ import annotations

import hmac
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.finance_core.domain.operational_guards import FinanceOperationalGuardError, FinanceOperationalPosture
from app.finance_core.domain.provider_boundary import FinanceProviderConfigError, ProviderCheckoutIntentRequest
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    RazorpaySandboxConfig,
    amount_to_razorpay_subunits,
    validate_razorpay_sandbox_config,
)
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter
from tests.finance_core.test_phase5c_invoice_engine import fetch_one
from tests.finance_core.test_phase5d_payment_ledger import seed_finance_foundation


class FakeRazorpaySandboxClient:
    def __init__(self):
        self.requests: list[RazorpayOrderCreateRequest] = []

    async def create_order(self, request: RazorpayOrderCreateRequest) -> RazorpayOrderCreateResponse:
        self.requests.append(request)
        return RazorpayOrderCreateResponse(
            order_id="order_test_123",
            amount_subunits=request.amount_subunits,
            currency_code=request.currency_code,
            receipt=request.receipt,
            status="created",
        )


def sandbox_config(
    *,
    mode: str = "sandbox",
    key_id: str = "rzp_test_key_id",
    key_secret: str = "rzp_test_key_secret",
    webhook_secret: str = "rzp_webhook_secret",
) -> RazorpaySandboxConfig:
    return RazorpaySandboxConfig(
        mode=mode,  # type: ignore[arg-type]
        key_id=key_id,
        key_secret=key_secret,
        webhook_secret=webhook_secret,
        merchant_reference="vitara_sandbox_merchant",
    )


def provider_request(*, amount: Decimal = Decimal("1180.00")) -> ProviderCheckoutIntentRequest:
    return ProviderCheckoutIntentRequest(
        invoice_id=uuid.UUID("00000000-0000-0000-0000-000000000123"),
        amount=amount,
        currency_code="INR",
        idempotency_key="phase6b-order-key",
    )


def test_razorpay_sandbox_config_accepts_sandbox_and_test_modes():
    assert validate_razorpay_sandbox_config(sandbox_config(mode="sandbox")).mode == "sandbox"
    assert validate_razorpay_sandbox_config(sandbox_config(mode="test")).mode == "test"


@pytest.mark.parametrize(
    "config",
    [
        sandbox_config(mode="live", key_secret="do-not-leak", webhook_secret="do-not-leak-webhook"),
        sandbox_config(key_id="rzp_live_key_id", key_secret="do-not-leak", webhook_secret="do-not-leak-webhook"),
    ],
)
def test_razorpay_sandbox_config_rejects_live_or_production_posture_without_leaking_secrets(config: RazorpaySandboxConfig):
    with pytest.raises(FinanceProviderConfigError) as exc:
        validate_razorpay_sandbox_config(config)

    rendered = str(exc.value)
    assert "do-not-leak" not in rendered
    assert "do-not-leak-webhook" not in rendered
    assert "[REDACTED]" in rendered


def test_razorpay_sandbox_config_redacts_key_secret_and_webhook_secret():
    rendered = repr(sandbox_config(key_secret="private-key-secret", webhook_secret="private-webhook-secret"))

    assert "private-key-secret" not in rendered
    assert "private-webhook-secret" not in rendered
    assert rendered.count("[REDACTED]") == 2


@pytest.mark.parametrize(
    ("amount", "subunits"),
    [(Decimal("1180.00"), 118000), (Decimal("99.99"), 9999), (Decimal("1.235"), 124)],
)
def test_amount_to_razorpay_subunits(amount: Decimal, subunits: int):
    assert amount_to_razorpay_subunits(amount) == subunits


@pytest.mark.asyncio
async def test_create_order_request_maps_server_invoice_data_only():
    client = FakeRazorpaySandboxClient()
    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=client)

    result = await adapter.create_checkout_intent(provider_request())

    assert result.provider_code == "razorpay_sandbox"
    assert result.provider_order_ref == "order_test_123"
    assert result.status == "created"
    assert len(client.requests) == 1
    order_request = client.requests[0]
    assert order_request.amount_subunits == 118000
    assert order_request.currency_code == "INR"
    assert order_request.receipt == "fin_00000000000000000000000000000123"
    assert order_request.notes == {
        "finance_invoice_id": "00000000-0000-0000-0000-000000000123",
        "finance_idempotency_key": "phase6b-order-key",
    }


@pytest.mark.asyncio
async def test_browser_checkout_payload_exposes_no_financial_authority_or_secrets():
    client = FakeRazorpaySandboxClient()
    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=client)
    result = await adapter.create_checkout_intent(provider_request())

    browser_payload = adapter.checkout_fields(order_id=result.provider_order_ref or "").to_browser_payload()

    assert browser_payload == {"key": "rzp_test_key_id", "order_id": "order_test_123"}
    forbidden_fields = {"amount", "currency", "tax", "customer_id", "provider_customer_ref", "key_secret", "webhook_secret"}
    assert forbidden_fields.isdisjoint(browser_payload)


@pytest.mark.asyncio
async def test_adapter_uses_injected_fake_client_and_never_imports_http_clients():
    client = FakeRazorpaySandboxClient()
    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=client)

    await adapter.create_checkout_intent(provider_request())

    assert len(client.requests) == 1
    finance_root = Path(__file__).resolve().parents[2] / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined


@pytest.mark.asyncio
async def test_operational_guards_block_live_provider_mode():
    client = FakeRazorpaySandboxClient()
    adapter = RazorpaySandboxAdapter(
        config=sandbox_config(),
        client=client,
        guard_service=FinanceOperationalGuardService(FinanceOperationalPosture(live_provider_enabled=True)),
    )

    with pytest.raises(FinanceOperationalGuardError):
        await adapter.create_checkout_intent(provider_request())
    assert client.requests == []


def test_signature_verification_accepts_valid_hmac_and_rejects_invalid_signature():
    adapter = RazorpaySandboxAdapter(config=sandbox_config(webhook_secret="hook-secret"), client=FakeRazorpaySandboxClient())
    raw_body = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test"}}}}'
    signature = hmac.digest(b"hook-secret", raw_body, "sha256").hex()

    assert adapter.verify_webhook_signature(raw_body=raw_body, signature=signature) is True
    assert adapter.verify_webhook_signature(raw_body=raw_body, signature="bad-signature") is False


@pytest.mark.asyncio
async def test_order_creation_does_not_capture_allocate_post_ledger_or_activate_subscription():
    await seed_finance_foundation()
    before = await _finance_counts()

    adapter = RazorpaySandboxAdapter(config=sandbox_config(), client=FakeRazorpaySandboxClient())
    await adapter.create_checkout_intent(provider_request())

    after = await _finance_counts()
    assert after == before


async def _finance_counts():
    return await fetch_one(
        """
        SELECT
            (SELECT count(*) FROM finance.payments WHERE status = 'captured') AS captured_payments,
            (SELECT count(*) FROM finance.payment_allocations) AS allocations,
            (SELECT count(*) FROM finance.ledger_entries) AS ledger_entries,
            (SELECT count(*) FROM finance.ledger_entry_lines) AS ledger_lines,
            (SELECT count(*) FROM information_schema.tables WHERE table_name = 'platform_subscriptions') AS subscription_tables
        """
    )


def test_phase6b_has_no_public_routes_frontend_subscription_or_live_provider_enablement():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "finance_payment_api_enabled = false" in combined
    assert "require_finance_payment_api_enabled" in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert "platform_subscriptions" not in combined
    assert "provider_secret" not in combined
    assert "rzp_live_" not in combined
    assert not (repo_root / "frontend").exists()
