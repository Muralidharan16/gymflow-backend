from __future__ import annotations

from decimal import Decimal
import uuid

import pytest

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    ProviderCheckoutIntentRequest,
)
from app.finance_core.domain.razorpay_live import RazorpayLiveConfig
from app.finance_core.services.razorpay_live import (
    RazorpayLiveCheckoutAdapter,
    RazorpayLiveOrdersClient,
)


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_json(self, **kwargs):
        self.calls.append(kwargs)
        payload = kwargs["payload"]
        return {
            "id": "order_pay24f_live_fixture",
            "amount": payload["amount"],
            "currency": payload["currency"],
            "receipt": payload["receipt"],
            "status": "created",
        }


def _config(**overrides) -> RazorpayLiveConfig:
    values = {
        "mode": "live",
        "key_id": "rzp_live_pay24f_public",
        "key_secret": "pay24f-private-secret-fixture",
        "merchant_reference": "doers-internal-canary",
        "timeout_seconds": Decimal("5.00"),
    }
    values.update(overrides)
    return RazorpayLiveConfig(**values)


@pytest.mark.asyncio
async def test_live_order_adapter_builds_one_fixed_origin_order_request() -> None:
    transport = _FakeTransport()
    config = _config()
    adapter = RazorpayLiveCheckoutAdapter(
        config=config,
        client=RazorpayLiveOrdersClient(
            config=config,
            transport=transport,
        ),
    )
    request = ProviderCheckoutIntentRequest(
        invoice_id=uuid.UUID("24f00000-0000-4000-8000-000000000001"),
        amount=Decimal("1.00"),
        currency_code="INR",
        idempotency_key="pay24f-live-order-fixture",
    )

    response = await adapter.create_checkout_intent(request)

    assert response.provider_code == "razorpay"
    assert response.provider_order_ref == "order_pay24f_live_fixture"
    assert response.status == "created"
    assert adapter.environment == "live"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.razorpay.com/v1/orders"
    assert call["payload"]["amount"] == 100
    assert call["payload"]["currency"] == "INR"
    assert call["payload"]["receipt"].startswith("fin_")
    assert call["headers"]["Authorization"].startswith("Basic ")


def test_live_config_rejects_test_key_before_transport() -> None:
    transport = _FakeTransport()
    with pytest.raises(FinanceProviderConfigError):
        RazorpayLiveOrdersClient(
            config=_config(key_id="rzp_test_pay24f"),
            transport=transport,
        )
    assert transport.calls == []


def test_live_config_repr_redacts_secret_and_key_id() -> None:
    config = _config()
    rendered = repr(config)
    assert "pay24f-private-secret-fixture" not in rendered
    assert "rzp_live_pay24f_public" not in rendered
    assert "[REDACTED]" in rendered
