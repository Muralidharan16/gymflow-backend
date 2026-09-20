from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.finance_core.domain.provider_boundary import (
    CheckoutProviderRegistry,
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    RazorpayProviderError,
    RazorpaySandboxConfig,
    classify_razorpay_provider_failure,
)
from app.finance_core.services.razorpay_sandbox import (
    RazorpaySandboxAdapter,
    RazorpayTestModeHTTPTransport,
    RazorpayTestModeOrdersClient,
)



ROOT = Path(__file__).resolve().parents[2]


def sandbox_config(
    *,
    mode: str = "test",
    key_id: str = "rzp_test_key_id",
    key_secret: str = "rzp_test_key_secret",
    webhook_secret: str = "rzp_test_webhook_secret",
) -> RazorpaySandboxConfig:
    return RazorpaySandboxConfig(
        mode=mode,  # type: ignore[arg-type]
        key_id=key_id,
        key_secret=key_secret,
        webhook_secret=webhook_secret,
        merchant_reference="pay7-test-merchant",
    )


class FakeGenericAdapter:
    def __init__(
        self,
        *,
        provider_code: str = "fake_provider",
        environment: str = "test",
    ):
        self.provider_code = provider_code
        self.environment = environment
        self.requests: list[ProviderCheckoutIntentRequest] = []

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        self.requests.append(request)
        return ProviderCheckoutIntentResponse(
            provider_code=self.provider_code,
            provider_order_ref="order_generic_1",
            status="created",
        )

    def build_checkout_fields(
        self,
        *,
        provider_order_ref: str,
    ) -> dict[str, str]:
        return {"order_id": provider_order_ref}


class FakeRazorpayClient:
    def __init__(self):
        self.requests: list[RazorpayOrderCreateRequest] = []

    async def create_order(
        self,
        request: RazorpayOrderCreateRequest,
    ) -> RazorpayOrderCreateResponse:
        self.requests.append(request)
        return RazorpayOrderCreateResponse(
            order_id="order_pay7_1",
            amount_subunits=request.amount_subunits,
            currency_code=request.currency_code,
            receipt=request.receipt,
            status="created",
        )


def provider_request() -> ProviderCheckoutIntentRequest:
    return ProviderCheckoutIntentRequest(
        invoice_id=uuid.UUID("77000000-0000-4000-8000-000000000001"),
        amount=Decimal("1180.00"),
        currency_code="INR",
        idempotency_key="pay7:create-checkout:1",
    )


def test_registry_is_server_owned_and_accepts_only_non_live_adapters():
    first = FakeGenericAdapter(provider_code="alpha_provider", environment="sandbox")
    second = FakeGenericAdapter(provider_code="beta_provider", environment="test")
    registry = CheckoutProviderRegistry((second, first))

    assert registry.provider_codes == ("alpha_provider", "beta_provider")
    assert registry.resolve("alpha_provider") is first
    assert registry.resolve("beta_provider") is second

    with pytest.raises(FinanceProviderConfigError):
        registry.resolve("unregistered_provider")
    with pytest.raises(FinanceProviderConfigError):
        CheckoutProviderRegistry(
            (FakeGenericAdapter(provider_code="live_provider", environment="live"),)
        )
    with pytest.raises(FinanceProviderConfigError):
        CheckoutProviderRegistry(
            (
                FakeGenericAdapter(provider_code="duplicate"),
                FakeGenericAdapter(provider_code="duplicate"),
            )
        )


def test_generic_provider_failure_semantics_are_mutually_safe():
    retryable = FinanceProviderOperationError(
        provider_code="fake",
        operation="create_checkout",
        code="CONNECT_FAILED",
        failure_class="retryable",
        message="Connection could not be established.",
    )
    final = FinanceProviderOperationError(
        provider_code="fake",
        operation="create_checkout",
        code="REQUEST_REJECTED",
        failure_class="final",
        message="Provider rejected the request.",
    )
    unknown = FinanceProviderOperationError(
        provider_code="fake",
        operation="create_checkout",
        code="OUTCOME_UNKNOWN",
        failure_class="unknown",
        message="Provider outcome is unknown.",
    )

    assert retryable.automatic_retry_allowed is True
    assert retryable.requires_reconciliation is False
    assert final.automatic_retry_allowed is False
    assert final.requires_reconciliation is False
    assert unknown.automatic_retry_allowed is False
    assert unknown.requires_reconciliation is True


@pytest.mark.parametrize(
    ("code", "status_code", "expected"),
    [
        ("RAZORPAY_CONNECT_FAILED", None, "retryable"),
        ("RAZORPAY_TIMEOUT", None, "unknown"),
        ("RAZORPAY_NETWORK_ERROR", None, "unknown"),
        ("RAZORPAY_RESPONSE_INVALID", None, "unknown"),
        ("RAZORPAY_ORDER_AMOUNT_MISMATCH", None, "unknown"),
        ("RAZORPAY_HTTP_ERROR", 500, "unknown"),
        ("RAZORPAY_HTTP_ERROR", 429, "unknown"),
        ("RAZORPAY_HTTP_ERROR", 409, "unknown"),
        ("RAZORPAY_HTTP_ERROR", 400, "final"),
        ("RAZORPAY_HTTP_ERROR", 401, "final"),
        ("RAZORPAY_ORDER_NOTES_UNSAFE", None, "final"),
    ],
)
def test_razorpay_failure_classification_is_conservative(
    code: str,
    status_code: int | None,
    expected: str,
):
    assert (
        classify_razorpay_provider_failure(
            code=code,
            provider_status_code=status_code,
        )
        == expected
    )


def test_razorpay_error_is_provider_neutral_error_subtype():
    exc = RazorpayProviderError(
        "RAZORPAY_HTTP_ERROR",
        "Razorpay request was rejected safely.",
        provider_status_code=400,
    )

    assert isinstance(exc, FinanceProviderOperationError)
    assert exc.provider_code == "razorpay_sandbox"
    assert exc.operation == "create_checkout"
    assert exc.failure_class == "final"
    assert exc.automatic_retry_allowed is False
    assert exc.requires_reconciliation is False
    assert "RAZORPAY_HTTP_ERROR" in str(exc)


class ConnectionFactoryFailure:
    def __call__(self, host: str, *, timeout: float):
        raise OSError("private-low-level-connect-material")


@pytest.mark.asyncio
async def test_pre_submit_connection_failure_is_retryable_and_sanitized():
    transport = RazorpayTestModeHTTPTransport(
        connection_factory=ConnectionFactoryFailure(),
    )
    client = RazorpayTestModeOrdersClient(
        config=sandbox_config(key_secret="pay7-private-key-secret"),
        transport=transport,
    )

    with pytest.raises(RazorpayProviderError) as caught:
        await client.create_order(
            RazorpayOrderCreateRequest(
                amount_subunits=118000,
                currency_code="INR",
                receipt="fin_pay7_connect",
                notes={"finance_invoice_id": str(uuid.uuid4())},
            )
        )

    error = caught.value
    assert error.code == "RAZORPAY_CONNECT_FAILED"
    assert error.failure_class == "retryable"
    assert error.automatic_retry_allowed is True
    assert "pay7-private-key-secret" not in str(error)
    assert "private-low-level-connect-material" not in str(error)


class FakeHTTPResponse:
    def __init__(self, *, status: int, payload: dict | bytes):
        self.status = status
        self._payload = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

    def read(self) -> bytes:
        return self._payload


class PostSubmitConnection:
    response = FakeHTTPResponse(
        status=500,
        payload={"error": "private-provider-response"},
    )
    request_error: Exception | None = None

    def __init__(self, host: str, *, timeout: float):
        self.host = host
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_post_submit_server_failure_is_unknown_not_retryable():
    PostSubmitConnection.request_error = None
    PostSubmitConnection.response = FakeHTTPResponse(
        status=500,
        payload={"error": "private-provider-response"},
    )
    transport = RazorpayTestModeHTTPTransport(
        connection_factory=PostSubmitConnection,
    )

    with pytest.raises(RazorpayProviderError) as caught:
        await transport.post_json(
            url="https://api.razorpay.com/v1/orders",
            headers={"Authorization": "Basic private-auth"},
            payload={"amount": 118000},
            timeout_seconds=5,
        )

    error = caught.value
    assert error.code == "RAZORPAY_HTTP_ERROR"
    assert error.failure_class == "unknown"
    assert error.automatic_retry_allowed is False
    assert error.requires_reconciliation is True
    assert "private-provider-response" not in str(error)
    assert "private-auth" not in str(error)


@pytest.mark.asyncio
async def test_post_submit_timeout_is_unknown_not_retryable():
    PostSubmitConnection.request_error = TimeoutError("private-timeout-detail")
    transport = RazorpayTestModeHTTPTransport(
        connection_factory=PostSubmitConnection,
    )
    client = RazorpayTestModeOrdersClient(
        config=sandbox_config(key_secret="pay7-private-key-secret"),
        transport=transport,
    )

    with pytest.raises(RazorpayProviderError) as caught:
        await client.create_order(
            RazorpayOrderCreateRequest(
                amount_subunits=118000,
                currency_code="INR",
                receipt="fin_pay7_timeout",
                notes={"finance_invoice_id": str(uuid.uuid4())},
            )
        )

    error = caught.value
    assert error.code == "RAZORPAY_TIMEOUT"
    assert error.failure_class == "unknown"
    assert error.automatic_retry_allowed is False
    assert error.requires_reconciliation is True
    assert "private-timeout-detail" not in str(error)
    assert "pay7-private-key-secret" not in str(error)
    PostSubmitConnection.request_error = None


@pytest.mark.asyncio
async def test_razorpay_adapter_satisfies_generic_checkout_contract():
    client = FakeRazorpayClient()
    adapter = RazorpaySandboxAdapter(
        config=sandbox_config(mode="test"),
        client=client,
    )
    registry = CheckoutProviderRegistry((adapter,))
    generic = registry.resolve("razorpay_sandbox")

    result = await generic.create_checkout_intent(provider_request())

    assert generic.environment == "test"
    assert result.provider_code == "razorpay_sandbox"
    assert result.provider_order_ref == "order_pay7_1"
    assert generic.build_checkout_fields(
        provider_order_ref=result.provider_order_ref,
    ) == {"key": "rzp_test_key_id", "order_id": "order_pay7_1"}


def test_business_orchestration_has_no_concrete_razorpay_dependency():
    for relative in (
        "app/finance_core/services/checkout_orchestration.py",
        "app/finance_core/services/member_subscription_checkout.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "from app.finance_core.services.razorpay_sandbox" not in source
        assert "from app.finance_core.domain.razorpay_sandbox" not in source
        assert "RazorpaySandboxAdapter" not in source
        assert "_razorpay_adapter" not in source
        assert "CheckoutIntentProvider" in source
        assert "_provider_adapter" in source or "adapter =" in source


def test_provider_adapter_layer_has_no_finance_persistence_authority():
    source = (
        ROOT / "app/finance_core/services/razorpay_sandbox.py"
    ).read_text(encoding="utf-8").lower()

    for forbidden in (
        "sqlalchemy",
        "asyncsession",
        "finance.payments",
        "finance.invoices",
        "payment_allocations",
        "ledger_entries",
        "subscription_terms",
        "update finance.",
        "insert into finance.",
    ):
        assert forbidden not in source


def test_composition_uses_server_registry_not_browser_provider_selection():
    payment_api = (
        ROOT / "app/finance_core/api/payment_boundary.py"
    ).read_text(encoding="utf-8")
    member_api = (
        ROOT / "app/routers/member_subscriptions_v2.py"
    ).read_text(encoding="utf-8")

    for source in (payment_api, member_api):
        assert "CheckoutProviderRegistry" in source
        assert "registry.resolve(" in source
    assert "request.provider_code" not in payment_api
    assert "data.provider_code" not in member_api


def test_no_live_provider_enablement_is_introduced():
    finance_root = ROOT / "app" / "finance_core"
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in finance_root.rglob("*.py")
    )
    assert 'providerenvironment = literal["sandbox", "test"]' in combined
    assert "pay-7 registry accepts sandbox/test adapters only" in combined
    assert "live_provider_enabled: bool = false" in combined
    assert "live_money_movement_enabled: bool = false" in combined
