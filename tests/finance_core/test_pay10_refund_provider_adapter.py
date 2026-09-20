from __future__ import annotations

import base64
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    ProviderRefundRequest,
    RefundProviderRegistry,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayProviderError,
    RazorpayRefundCreateRequest,
    build_razorpay_refund_receipt,
    map_razorpay_refund_response,
)
from app.finance_core.services.razorpay_sandbox import (
    RazorpaySandboxRefundAdapter,
    RazorpayTestModeRefundsClient,
)
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import (
    sandbox_config,
)


COMMAND_ID = uuid.UUID("b1000000-0000-4000-8000-000000000001")
REFUND_ID = uuid.UUID("b1000000-0000-4000-8000-000000000002")
PAYMENT_REF = "pay_PAY10B123"
REFUND_REF = "rfnd_PAY10B123"


class FakeRefundTransport:
    def __init__(
        self,
        *,
        post_response: dict[str, Any] | None = None,
        get_response: dict[str, Any] | None = None,
        post_error: Exception | None = None,
        get_error: Exception | None = None,
    ):
        self.post_response = post_response
        self.get_response = get_response
        self.post_error = post_error
        self.get_error = get_error
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.posts.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.post_error is not None:
            raise self.post_error
        return self.post_response or _provider_payload()

    async def get_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.gets.append(
            {
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.get_error is not None:
            raise self.get_error
        return self.get_response or _provider_payload()


def _request(
    *,
    amount: Decimal = Decimal("25.50"),
    currency: str = "INR",
    payment_ref: str = PAYMENT_REF,
) -> ProviderRefundRequest:
    return ProviderRefundRequest(
        command_id=COMMAND_ID,
        refund_id=REFUND_ID,
        provider_payment_ref=payment_ref,
        amount=amount,
        currency_code=currency,
    )


def _provider_request() -> RazorpayRefundCreateRequest:
    return RazorpayRefundCreateRequest(
        provider_payment_ref=PAYMENT_REF,
        amount_subunits=2550,
        currency_code="INR",
        receipt=build_razorpay_refund_receipt(str(COMMAND_ID)),
        notes={
            "finance_refund_id": str(REFUND_ID),
            "finance_refund_command_id": str(COMMAND_ID),
        },
    )


def _provider_payload(
    *,
    refund_ref: str = REFUND_REF,
    payment_ref: str = PAYMENT_REF,
    amount: int = 2550,
    currency: str = "INR",
    receipt: str | None = None,
    status: str = "processed",
    created_at: int = 1789900000,
) -> dict[str, Any]:
    return {
        "id": refund_ref,
        "entity": "refund",
        "amount": amount,
        "currency": currency,
        "payment_id": payment_ref,
        "receipt": receipt or build_razorpay_refund_receipt(str(COMMAND_ID)),
        "status": status,
        "created_at": created_at,
    }


def _adapter(
    transport: FakeRefundTransport,
) -> RazorpaySandboxRefundAdapter:
    config = sandbox_config(key_secret="pay10b_private_test_secret")
    return RazorpaySandboxRefundAdapter(
        config=config,
        client=RazorpayTestModeRefundsClient(
            config=config,
            transport=transport,
        ),
    )


def test_pay10b_refund_receipt_is_deterministic_and_command_bound():
    first = build_razorpay_refund_receipt(str(COMMAND_ID))
    second = build_razorpay_refund_receipt(str(COMMAND_ID))

    assert first == second
    assert first == "d10_b1000000000040008000000000000001"
    assert len(first) == 36

    with pytest.raises(RazorpayProviderError) as exc:
        build_razorpay_refund_receipt("not-a-uuid")
    assert exc.value.failure_class == "final"


@pytest.mark.asyncio
async def test_pay10b_create_refund_uses_exact_safe_provider_request():
    transport = FakeRefundTransport()
    response = await _adapter(transport).create_refund(_request())

    assert response.provider_code == "razorpay_sandbox"
    assert response.provider_payment_ref == PAYMENT_REF
    assert response.provider_refund_ref == REFUND_REF
    assert response.status == "processed"
    assert response.amount == Decimal("25.50")
    assert response.currency_code == "INR"
    assert response.receipt == build_razorpay_refund_receipt(str(COMMAND_ID))

    assert len(transport.posts) == 1
    call = transport.posts[0]
    assert call["url"] == (
        "https://api.razorpay.com/v1/payments/"
        f"{PAYMENT_REF}/refund"
    )
    assert call["payload"] == {
        "amount": 2550,
        "speed": "normal",
        "receipt": build_razorpay_refund_receipt(str(COMMAND_ID)),
        "notes": {
            "finance_refund_id": str(REFUND_ID),
            "finance_refund_command_id": str(COMMAND_ID),
        },
    }
    token = call["headers"]["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode("utf-8") == (
        "rzp_test_key_id:pay10b_private_test_secret"
    )
    rendered = str(call["payload"]).lower()
    assert "private_test_secret" not in rendered
    assert "email" not in rendered
    assert "phone" not in rendered
    assert "token" not in rendered


@pytest.mark.parametrize("status", ["pending", "processed", "failed"])
@pytest.mark.asyncio
async def test_pay10b_all_documented_refund_states_normalize(status: str):
    transport = FakeRefundTransport(
        post_response=_provider_payload(status=status)
    )
    response = await _adapter(transport).create_refund(_request())
    assert response.status == status


@pytest.mark.parametrize(
    "payload",
    [
        {**_provider_payload(), "entity": "payment"},
        _provider_payload(refund_ref="bad_ref"),
        _provider_payload(payment_ref="pay_OTHER123"),
        _provider_payload(amount=1),
        _provider_payload(currency="USD"),
        _provider_payload(receipt="wrong_receipt"),
        _provider_payload(status="mystery"),
        _provider_payload(created_at=-1),
    ],
)
def test_pay10b_provider_response_mismatch_is_unknown_not_retryable(
    payload: dict[str, Any],
):
    with pytest.raises(RazorpayProviderError) as exc:
        map_razorpay_refund_response(
            payload=payload,
            expected=_provider_request(),
        )

    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True
    assert exc.value.automatic_retry_allowed is False
    assert "pay10b_private_test_secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_pay10b_timeout_after_submission_requires_reconciliation():
    transport = FakeRefundTransport(
        post_error=TimeoutError("pay10b_private_test_secret timeout")
    )
    with pytest.raises(RazorpayProviderError) as exc:
        await _adapter(transport).create_refund(_request())

    assert exc.value.code == "RAZORPAY_REFUND_TIMEOUT"
    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True
    assert exc.value.automatic_retry_allowed is False
    assert "pay10b_private_test_secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_pay10b_connect_before_request_is_retryable():
    transport = FakeRefundTransport(
        post_error=RazorpayProviderError(
            "RAZORPAY_CONNECT_FAILED",
            "Safe connect failure.",
            failure_class="retryable",
        )
    )
    with pytest.raises(RazorpayProviderError) as exc:
        await _adapter(transport).create_refund(_request())

    assert exc.value.code == "RAZORPAY_REFUND_CONNECT_FAILED"
    assert exc.value.failure_class == "retryable"
    assert exc.value.automatic_retry_allowed is True
    assert exc.value.requires_reconciliation is False


@pytest.mark.asyncio
async def test_pay10b_fetch_reconciliation_uses_exact_provider_identity():
    transport = FakeRefundTransport(
        get_response=_provider_payload(status="pending")
    )
    response = await _adapter(transport).fetch_refund(
        _request(),
        provider_refund_ref=REFUND_REF,
    )

    assert response.status == "pending"
    assert len(transport.posts) == 0
    assert len(transport.gets) == 1
    assert transport.gets[0]["url"] == (
        "https://api.razorpay.com/v1/payments/"
        f"{PAYMENT_REF}/refunds/{REFUND_REF}"
    )


@pytest.mark.asyncio
async def test_pay10b_fetch_404_is_unknown_not_proof_of_non_acceptance():
    transport = FakeRefundTransport(
        get_error=RazorpayProviderError(
            "RAZORPAY_HTTP_ERROR",
            "safe not found",
            provider_status_code=404,
            failure_class="final",
        )
    )
    with pytest.raises(RazorpayProviderError) as exc:
        await _adapter(transport).fetch_refund(
            _request(),
            provider_refund_ref=REFUND_REF,
        )

    assert exc.value.code == "RAZORPAY_REFUND_NOT_FOUND"
    assert exc.value.provider_status_code == 404
    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True


@pytest.mark.parametrize(
    ("request", "expected_code"),
    [
        (_request(currency="USD"), "RAZORPAY_REFUND_CURRENCY_UNSUPPORTED"),
        (_request(amount=Decimal("0.00")), "RAZORPAY_REFUND_AMOUNT_INVALID"),
        (_request(payment_ref="not-a-payment"), "RAZORPAY_PAYMENT_REF_INVALID"),
    ],
)
@pytest.mark.asyncio
async def test_pay10b_invalid_finance_to_provider_shape_fails_before_network(
    request: ProviderRefundRequest,
    expected_code: str,
):
    transport = FakeRefundTransport()
    with pytest.raises(RazorpayProviderError) as exc:
        await _adapter(transport).create_refund(request)

    assert exc.value.code == expected_code
    assert exc.value.failure_class == "final"
    assert transport.posts == []


def test_pay10b_refund_registry_is_server_owned_and_test_mode_only():
    adapter = _adapter(FakeRefundTransport())
    registry = RefundProviderRegistry((adapter,))

    assert registry.provider_codes == ("razorpay_sandbox",)
    assert registry.resolve("razorpay_sandbox") is adapter

    with pytest.raises(FinanceProviderConfigError):
        RefundProviderRegistry((adapter, adapter))

    class LiveAdapter:
        provider_code = "razorpay_live"
        environment = "live"

        async def create_refund(self, request):
            raise AssertionError

        async def fetch_refund(self, request, *, provider_refund_ref):
            raise AssertionError

    with pytest.raises(FinanceProviderConfigError):
        RefundProviderRegistry((LiveAdapter(),))


def test_pay10b_refund_adapter_is_not_wired_to_api_or_worker_routes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "app/finance_core/api",
        root / "app/routers",
        root / "app/tasks",
    ]
    matches = []
    for target in targets:
        if not target.exists():
            continue
        for path in target.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if (
                "RazorpaySandboxRefundAdapter" in source
                or "RazorpayTestModeRefundsClient" in source
            ):
                matches.append(str(path.relative_to(root)))

    assert matches == []


def test_pay10b_has_no_live_refund_key_or_live_money_enablement():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    paths = (
        root / "app/finance_core/domain/provider_boundary.py",
        root / "app/finance_core/domain/razorpay_sandbox.py",
        root / "app/finance_core/services/razorpay_sandbox.py",
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in paths
    )
    assert "rzp_live_" not in combined
    assert "live_money_movement_enabled=true" not in combined.replace(" ", "")
    assert "activate_subscription" not in combined
