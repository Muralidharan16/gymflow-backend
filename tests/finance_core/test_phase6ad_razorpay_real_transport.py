from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from app.finance_core.domain.provider_boundary import FinanceProviderConfigError
from app.finance_core.domain.razorpay_sandbox import RazorpayProviderError, RazorpaySandboxConfig, validate_razorpay_sandbox_config
from app.finance_core.services.razorpay_sandbox import RazorpayTestModeHTTPTransport, RazorpayTestModeOrdersClient
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config
from tests.finance_core.test_phase6u_razorpay_test_mode_adapter import order_request
from tests.finance_core.test_phase6h_disabled_payment_api_routes import assert_disabled, finance_counts


class FakeHTTPResponse:
    def __init__(self, *, status: int = 200, payload: dict | list | bytes | None = None):
        self.status = status
        if isinstance(payload, bytes):
            self._body = payload
        else:
            self._body = json.dumps(
                payload
                if payload is not None
                else {
                    "id": "order_phase6ad_test",
                    "amount": 118000,
                    "currency": "INR",
                    "receipt": "fin_000000000000000000000000000006a0",
                    "status": "created",
                }
            ).encode("utf-8")

    def read(self) -> bytes:
        return self._body


class FakeHTTPSConnection:
    instances: list["FakeHTTPSConnection"] = []
    response = FakeHTTPResponse()
    error: Exception | None = None

    def __init__(self, host: str, *, timeout: float):
        self.host = host
        self.timeout = timeout
        self.requests: list[dict] = []
        self.closed = False
        FakeHTTPSConnection.instances.append(self)

    def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers})
        if FakeHTTPSConnection.error is not None:
            raise FakeHTTPSConnection.error

    def getresponse(self) -> FakeHTTPResponse:
        return FakeHTTPSConnection.response

    def close(self) -> None:
        self.closed = True


def reset_fake_connection(*, response: FakeHTTPResponse | None = None, error: Exception | None = None) -> None:
    FakeHTTPSConnection.instances = []
    FakeHTTPSConnection.response = response or FakeHTTPResponse()
    FakeHTTPSConnection.error = error


@pytest.mark.asyncio
async def test_phase6ad_real_transport_constructs_safe_orders_request_with_fake_connection_only():
    reset_fake_connection()
    config = sandbox_config(key_secret="phase6ad_private_key_secret")
    transport = RazorpayTestModeHTTPTransport(connection_factory=FakeHTTPSConnection)
    client = RazorpayTestModeOrdersClient(config=config, transport=transport)

    response = await client.create_order(order_request())

    assert response.order_id == "order_phase6ad_test"
    assert len(FakeHTTPSConnection.instances) == 1
    connection = FakeHTTPSConnection.instances[0]
    assert connection.host == "api.razorpay.com"
    assert connection.timeout == 5.0
    assert connection.closed is True
    assert len(connection.requests) == 1
    request = connection.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/orders"
    assert json.loads(request["body"].decode("utf-8")) == {
        "amount": 118000,
        "currency": "INR",
        "notes": {
            "finance_idempotency_key": "phase6u-order",
            "finance_invoice_id": "00000000-0000-0000-0000-0000000006a0",
        },
        "receipt": "fin_000000000000000000000000000006a0",
    }
    token = request["headers"]["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode("utf-8") == "rzp_test_key_id:phase6ad_private_key_secret"
    assert request["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_phase6ad_real_transport_rejects_unapproved_url_and_timeout_before_connection():
    reset_fake_connection()
    transport = RazorpayTestModeHTTPTransport(connection_factory=FakeHTTPSConnection)

    with pytest.raises(RazorpayProviderError) as unsafe_url:
        await transport.post_json(
            url="https://example.invalid/v1/orders",
            headers={"Authorization": "Basic not-reported"},
            payload={"amount": 1},
            timeout_seconds=5,
        )
    with pytest.raises(RazorpayProviderError) as unsafe_timeout:
        await transport.post_json(
            url="https://api.razorpay.com/v1/orders",
            headers={"Authorization": "Basic not-reported"},
            payload={"amount": 1},
            timeout_seconds=0,
        )

    assert unsafe_url.value.code == "RAZORPAY_URL_UNSAFE"
    assert unsafe_timeout.value.code == "RAZORPAY_TIMEOUT_UNSAFE"
    assert FakeHTTPSConnection.instances == []


@pytest.mark.parametrize(
    "config",
    [
        sandbox_config(mode="live", key_secret="phase6ad_private_key_secret", webhook_secret="phase6ad_private_webhook_secret"),
        sandbox_config(key_id="rzp_live_phase6ad", key_secret="phase6ad_private_key_secret", webhook_secret="phase6ad_private_webhook_secret"),
        sandbox_config(key_secret="", webhook_secret="phase6ad_private_webhook_secret"),
        sandbox_config(key_secret="rzp_live_phase6ad_secret", webhook_secret="phase6ad_private_webhook_secret"),
        RazorpaySandboxConfig(
            mode="test",
            key_id="rzp_test_phase6ad",
            key_secret="phase6ad_private_key_secret",
            webhook_secret="phase6ad_private_webhook_secret",
            merchant_reference="phase6ad",
            api_base_url="https://unsafe.example.invalid/v1",
        ),
    ],
)
def test_phase6ad_config_rejects_live_or_unsafe_values_without_secret_leak(config):
    with pytest.raises(FinanceProviderConfigError) as exc:
        validate_razorpay_sandbox_config(config)

    rendered = str(exc.value)
    assert "phase6ad_private_key_secret" not in rendered
    assert "phase6ad_private_webhook_secret" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.asyncio
async def test_phase6ad_transport_errors_are_sanitized_and_do_not_expose_authorization_or_secret():
    reset_fake_connection(error=RuntimeError("phase6ad_private_key_secret low-level failure"))
    config = sandbox_config(key_secret="phase6ad_private_key_secret")
    client = RazorpayTestModeOrdersClient(
        config=config,
        transport=RazorpayTestModeHTTPTransport(connection_factory=FakeHTTPSConnection),
    )

    with pytest.raises(RazorpayProviderError) as exc:
        await client.create_order(order_request())

    rendered = str(exc.value)
    assert "RAZORPAY_NETWORK_ERROR" in rendered
    assert "phase6ad_private_key_secret" not in rendered
    assert "Authorization" not in rendered
    assert "Basic" not in rendered


@pytest.mark.asyncio
async def test_phase6ad_http_and_invalid_json_errors_are_sanitized():
    for response in (
        FakeHTTPResponse(status=401, payload={"error": "phase6ad_private_key_secret"}),
        FakeHTTPResponse(status=200, payload=b"not-json-phase6ad_private_key_secret"),
    ):
        reset_fake_connection(response=response)
        client = RazorpayTestModeOrdersClient(
            config=sandbox_config(key_secret="phase6ad_private_key_secret"),
            transport=RazorpayTestModeHTTPTransport(connection_factory=FakeHTTPSConnection),
        )
        with pytest.raises(RazorpayProviderError) as exc:
            await client.create_order(order_request())
        rendered = str(exc.value)
        assert "phase6ad_private_key_secret" not in rendered
        assert "Authorization" not in rendered
        assert "Basic" not in rendered


@pytest.mark.asyncio
async def test_phase6ad_default_routes_remain_disabled_and_do_not_construct_real_transport(client):
    reset_fake_connection()
    before = await finance_counts()
    response = await client.post(
        "/api/v1/finance/payments/checkout-sessions",
        headers={"Authorization": "Bearer phase6ad", "X-Idempotency-Key": "phase6ad-disabled"},
        json={
            "plan_code": "DOERS_PRO_MONTHLY",
            "billing_interval": "monthly",
            "billing_party_id": "00000000-0000-0000-0000-000000000001",
        },
    )

    assert response.status_code in {401, 404}
    if response.status_code == 404:
        assert_disabled(response)
    assert await finance_counts() == before
    assert FakeHTTPSConnection.instances == []


def test_phase6ad_source_scope_has_no_sdk_dependency_frontend_or_subscription_behavior():
    repo_root = Path(__file__).resolve().parents[2]
    finance_root = repo_root / "app" / "finance_core"
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in finance_root.rglob("*.py"))

    assert "http.client" in combined
    assert "razorpay.client" not in combined
    assert "razorpayclient" not in combined
    assert "requests" not in combined
    assert "httpx" not in combined
    assert "aiohttp" not in combined
    assert "urllib" not in combined
    assert "provider_secret" not in combined
    assert "activate_subscription" not in combined
    assert "deactivate_subscription" not in combined
    assert not (repo_root / "frontend").exists()
