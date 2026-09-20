from __future__ import annotations

import base64
import json
import uuid
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from app.finance_core.domain.provider_boundary import (
    FinanceProviderOperationError,
    ProviderRefundRequest,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayProviderError,
    RazorpayRefundRequest,
    map_razorpay_refund_response,
)
from app.finance_core.services.razorpay_sandbox import (
    RazorpaySandboxAdapter,
    RazorpayTestModeHTTPTransport,
    RazorpayTestModeRefundsClient,
)
from tests.finance_core.test_phase6b_razorpay_sandbox_adapter import sandbox_config


COMMAND_ID = uuid.UUID("a1000000-0000-4000-8000-000000000101")
REFUND_ID = uuid.UUID("a1000000-0000-4000-8000-000000000102")
PAYMENT_ID = uuid.UUID("a1000000-0000-4000-8000-000000000103")
PROVIDER_PAYMENT_ID = "pay_PAY10ProviderPayment"
PROVIDER_REFUND_ID = "rfnd_PAY10ProviderRefund"


def provider_refund_request(
    *,
    amount: Decimal = Decimal("1180.00"),
    currency_code: str = "INR",
    provider_payment_ref: str = PROVIDER_PAYMENT_ID,
) -> ProviderRefundRequest:
    return ProviderRefundRequest(
        command_id=COMMAND_ID,
        refund_id=REFUND_ID,
        payment_id=PAYMENT_ID,
        provider_payment_ref=provider_payment_ref,
        amount=amount,
        currency_code=currency_code,
    )


class FakeOrderClient:
    async def create_order(self, request):
        raise AssertionError("checkout path is not part of PAY-10-B refund tests")


class FakeRefundTransport:
    def __init__(
        self,
        *,
        submit_response: dict[str, Any] | None = None,
        fetch_response: dict[str, Any] | None = None,
        submit_error: Exception | None = None,
        fetch_error: Exception | None = None,
    ):
        self.submit_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.submit_response = submit_response
        self.fetch_response = fetch_response
        self.submit_error = submit_error
        self.fetch_error = fetch_error

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.submit_calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.submit_error is not None:
            raise self.submit_error
        return self.submit_response or {
            "id": PROVIDER_REFUND_ID,
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": payload["amount"],
            "currency": "INR",
            "receipt": payload["receipt"],
            "status": "processed",
        }

    async def get_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.fetch_calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.fetch_response is not None:
            return self.fetch_response
        raise AssertionError("fetch_response is required for reconciliation tests")


def build_adapter(transport: FakeRefundTransport) -> RazorpaySandboxAdapter:
    config = sandbox_config(key_secret="pay10_private_test_secret")
    refund_client = RazorpayTestModeRefundsClient(
        config=config,
        transport=transport,
    )
    return RazorpaySandboxAdapter(
        config=config,
        client=FakeOrderClient(),
        refund_client=refund_client,
    )


def test_pay10_b_provider_refund_request_is_immutable():
    request = provider_refund_request()
    with pytest.raises(FrozenInstanceError):
        request.amount = Decimal("1.00")  # type: ignore[misc]


def test_pay10_b_refund_receipt_is_deterministic_and_server_derived():
    adapter = build_adapter(FakeRefundTransport())
    request = provider_refund_request()

    first = adapter.build_refund_request(request)
    second = adapter.build_refund_request(request)

    assert first.receipt == second.receipt
    assert first.receipt.startswith("rf_")
    assert len(first.receipt) == 35
    assert str(REFUND_ID) not in first.receipt
    assert str(COMMAND_ID) not in first.receipt
    assert first.notes == {
        "finance_command_id": str(COMMAND_ID),
        "finance_refund_id": str(REFUND_ID),
        "finance_payment_id": str(PAYMENT_ID),
    }


@pytest.mark.asyncio
async def test_pay10_b_submit_refund_uses_exact_payment_amount_receipt_and_safe_basic_auth():
    transport = FakeRefundTransport()
    adapter = build_adapter(transport)

    response = await adapter.submit_refund(provider_refund_request())

    assert response.provider_code == "razorpay_sandbox"
    assert response.provider_refund_ref == PROVIDER_REFUND_ID
    assert response.provider_payment_ref == PROVIDER_PAYMENT_ID
    assert response.amount == Decimal("1180")
    assert response.currency_code == "INR"
    assert response.status == "processed"

    assert len(transport.submit_calls) == 1
    call = transport.submit_calls[0]
    assert call["url"] == (
        "https://api.razorpay.com/v1/payments/"
        f"{PROVIDER_PAYMENT_ID}/refund"
    )
    assert call["payload"] == {
        "amount": 118000,
        "speed": "normal",
        "receipt": response.receipt,
        "notes": {
            "finance_command_id": str(COMMAND_ID),
            "finance_refund_id": str(REFUND_ID),
            "finance_payment_id": str(PAYMENT_ID),
        },
    }
    assert call["timeout_seconds"] == 5.0
    token = call["headers"]["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode("utf-8") == (
        "rzp_test_key_id:pay10_private_test_secret"
    )

    rendered_payload = json.dumps(call["payload"], sort_keys=True).lower()
    for forbidden in (
        "pay10_private_test_secret",
        "webhook_secret",
        "email",
        "phone",
        "card",
        "cvv",
    ):
        assert forbidden not in rendered_payload


@pytest.mark.parametrize("status", ["pending", "processed", "failed"])
@pytest.mark.asyncio
async def test_pay10_b_normalizes_only_documented_refund_states(status: str):
    transport = FakeRefundTransport(
        submit_response={
            "id": PROVIDER_REFUND_ID,
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": 118000,
            "currency": "inr",
            "receipt": "placeholder",
            "status": status,
        }
    )
    adapter = build_adapter(transport)
    request = provider_refund_request()
    expected = adapter.build_refund_request(request)
    transport.submit_response["receipt"] = expected.receipt  # type: ignore[index]

    response = await adapter.submit_refund(request)

    assert response.status == status
    assert response.currency_code == "INR"


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_code"),
    [
        ("id", "pay_not_refund", "RAZORPAY_REFUND_ID_INVALID"),
        ("id", "rfnd_bad/path", "RAZORPAY_REFUND_ID_INVALID"),
        ("payment_id", "pay_wrong", "RAZORPAY_REFUND_PAYMENT_MISMATCH"),
        ("amount", 1, "RAZORPAY_REFUND_AMOUNT_MISMATCH"),
        ("currency", "USD", "RAZORPAY_REFUND_CURRENCY_MISMATCH"),
        ("receipt", "wrong-receipt", "RAZORPAY_REFUND_RECEIPT_MISMATCH"),
        ("status", "mystery", "RAZORPAY_REFUND_STATUS_INVALID"),
    ],
)
@pytest.mark.asyncio
async def test_pay10_b_provider_response_mismatch_is_unknown_and_requires_reconciliation(
    field: str,
    bad_value: object,
    expected_code: str,
):
    adapter = build_adapter(FakeRefundTransport())
    request = provider_refund_request()
    expected = adapter.build_refund_request(request)
    payload: dict[str, Any] = {
        "id": PROVIDER_REFUND_ID,
        "payment_id": PROVIDER_PAYMENT_ID,
        "amount": 118000,
        "currency": "INR",
        "receipt": expected.receipt,
        "status": "processed",
    }
    payload[field] = bad_value

    with pytest.raises(RazorpayProviderError) as exc:
        map_razorpay_refund_response(payload=payload, expected=expected)

    assert exc.value.code == expected_code
    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True
    assert exc.value.automatic_retry_allowed is False


@pytest.mark.asyncio
async def test_pay10_b_duplicate_receipt_http_400_is_ambiguous_not_blind_retry():
    transport = FakeRefundTransport(
        submit_error=RazorpayProviderError(
            "RAZORPAY_HTTP_ERROR",
            "safe provider error",
            provider_status_code=400,
        )
    )
    adapter = build_adapter(transport)

    with pytest.raises(FinanceProviderOperationError) as exc:
        await adapter.submit_refund(provider_refund_request())

    assert exc.value.operation == "submit_refund"
    assert exc.value.provider_status_code == 400
    assert exc.value.failure_class == "unknown"
    assert exc.value.requires_reconciliation is True
    assert exc.value.automatic_retry_allowed is False


@pytest.mark.asyncio
async def test_pay10_b_connect_before_request_is_retryable_but_timeout_is_unknown():
    retryable = build_adapter(
        FakeRefundTransport(
            submit_error=RazorpayProviderError(
                "RAZORPAY_CONNECT_FAILED",
                "connection not established",
                operation="submit_refund",
            )
        )
    )
    with pytest.raises(FinanceProviderOperationError) as connect_exc:
        await retryable.submit_refund(provider_refund_request())
    assert connect_exc.value.failure_class == "retryable"
    assert connect_exc.value.automatic_retry_allowed is True
    assert connect_exc.value.requires_reconciliation is False

    unknown = build_adapter(
        FakeRefundTransport(
            submit_error=TimeoutError("private transport detail"),
        )
    )
    with pytest.raises(FinanceProviderOperationError) as timeout_exc:
        await unknown.submit_refund(provider_refund_request())
    assert timeout_exc.value.code == "RAZORPAY_TIMEOUT"
    assert timeout_exc.value.failure_class == "unknown"
    assert timeout_exc.value.requires_reconciliation is True
    assert "private transport detail" not in str(timeout_exc.value)


@pytest.mark.asyncio
async def test_pay10_b_fetch_reconciles_exact_known_refund_without_new_post():
    adapter = build_adapter(FakeRefundTransport())
    request = provider_refund_request()
    expected = adapter.build_refund_request(request)
    transport = FakeRefundTransport(
        fetch_response={
            "id": PROVIDER_REFUND_ID,
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": 118000,
            "currency": "INR",
            "receipt": expected.receipt,
            "status": "processed",
        }
    )
    adapter = build_adapter(transport)

    response = await adapter.fetch_refund(
        request,
        provider_refund_ref=PROVIDER_REFUND_ID,
    )

    assert response.provider_refund_ref == PROVIDER_REFUND_ID
    assert response.status == "processed"
    assert transport.submit_calls == []
    assert len(transport.fetch_calls) == 1
    assert transport.fetch_calls[0]["url"] == (
        "https://api.razorpay.com/v1/payments/"
        f"{PROVIDER_PAYMENT_ID}/refunds/{PROVIDER_REFUND_ID}"
    )


@pytest.mark.asyncio
async def test_pay10_b_fetch_refuses_provider_refund_identity_substitution():
    request = provider_refund_request()
    preflight_adapter = build_adapter(FakeRefundTransport())
    expected = preflight_adapter.build_refund_request(request)
    transport = FakeRefundTransport(
        fetch_response={
            "id": "rfnd_DifferentRefund",
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": 118000,
            "currency": "INR",
            "receipt": expected.receipt,
            "status": "processed",
        }
    )
    adapter = build_adapter(transport)

    with pytest.raises(FinanceProviderOperationError) as exc:
        await adapter.fetch_refund(
            request,
            provider_refund_ref=PROVIDER_REFUND_ID,
        )

    assert exc.value.code == "RAZORPAY_REFUND_ID_INVALID"
    assert exc.value.requires_reconciliation is True


@pytest.mark.parametrize(
    "request",
    [
        provider_refund_request(provider_payment_ref="../refund"),
        provider_refund_request(provider_payment_ref="pay_"),
        provider_refund_request(provider_payment_ref="pay_ValidRef "),
        provider_refund_request(amount=Decimal("0")),
        provider_refund_request(amount=Decimal("0.001")),
        provider_refund_request(amount=Decimal("-1.00")),
        provider_refund_request(currency_code="INR/../"),
    ],
)
@pytest.mark.asyncio
async def test_pay10_b_invalid_server_authority_fails_before_provider_call(
    request: ProviderRefundRequest,
):
    transport = FakeRefundTransport()
    adapter = build_adapter(transport)

    with pytest.raises(FinanceProviderOperationError) as exc:
        await adapter.submit_refund(request)

    assert exc.value.failure_class == "final"
    assert transport.submit_calls == []


class FakeHTTPResponse:
    def __init__(self, *, status: int, payload: dict[str, Any]):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


class FakeHTTPSConnection:
    instances: list["FakeHTTPSConnection"] = []
    response = FakeHTTPResponse(
        status=200,
        payload={
            "id": PROVIDER_REFUND_ID,
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": 118000,
            "currency": "INR",
            "receipt": "placeholder",
            "status": "processed",
        },
    )

    def __init__(self, host: str, *, timeout: float):
        self.host = host
        self.timeout = timeout
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers}
        )

    def getresponse(self) -> FakeHTTPResponse:
        return self.__class__.response

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pay10_b_http_transport_fetch_uses_get_and_empty_body_only():
    FakeHTTPSConnection.instances = []
    transport = RazorpayTestModeHTTPTransport(
        connection_factory=FakeHTTPSConnection,
    )

    parsed = await transport.get_json(
        url=(
            "https://api.razorpay.com/v1/payments/"
            f"{PROVIDER_PAYMENT_ID}/refunds/{PROVIDER_REFUND_ID}"
        ),
        headers={
            "Authorization": "Basic test",
            "Content-Type": "application/json",
        },
        timeout_seconds=5.0,
    )

    assert parsed["id"] == PROVIDER_REFUND_ID
    assert len(FakeHTTPSConnection.instances) == 1
    connection = FakeHTTPSConnection.instances[0]
    assert connection.closed is True
    assert connection.requests == [
        {
            "method": "GET",
            "path": (
                "/v1/payments/"
                f"{PROVIDER_PAYMENT_ID}/refunds/{PROVIDER_REFUND_ID}"
            ),
            "body": b"",
            "headers": {
                "Authorization": "Basic test",
                "Content-Type": "application/json",
            },
        }
    ]


def test_pay10_b_safe_refund_output_excludes_raw_provider_payload_and_secrets():
    expected = RazorpayRefundRequest(
        provider_payment_id=PROVIDER_PAYMENT_ID,
        amount_subunits=118000,
        currency_code="INR",
        receipt="rf_safe",
        notes={"finance_refund_id": str(REFUND_ID)},
    )
    result = map_razorpay_refund_response(
        payload={
            "id": PROVIDER_REFUND_ID,
            "payment_id": PROVIDER_PAYMENT_ID,
            "amount": 118000,
            "currency": "INR",
            "receipt": "rf_safe",
            "status": "pending",
            "notes": {"provider_private": "must-not-escape"},
            "acquirer_data": {"arn": "private-provider-tracking"},
        },
        expected=expected,
    )

    safe = result.to_safe_output()
    assert safe == {
        "provider_refund_id": PROVIDER_REFUND_ID,
        "provider_payment_id": PROVIDER_PAYMENT_ID,
        "amount_subunits": 118000,
        "currency_code": "INR",
        "receipt": "rf_safe",
        "status": "pending",
    }
    rendered = str(safe).lower()
    assert "provider_private" not in rendered
    assert "private-provider-tracking" not in rendered
