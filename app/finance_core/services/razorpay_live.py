"""Razorpay live order transport/adapter for PAY-24-F."""

from __future__ import annotations

import base64
import http.client
import json
from decimal import Decimal
from typing import Any, Protocol

from app.finance_core.domain.provider_boundary import (
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.domain.razorpay_live import (
    RazorpayLiveConfig,
    RazorpayLiveProviderError,
    map_razorpay_live_order_response,
    validate_razorpay_live_config,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutFields,
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    amount_to_razorpay_subunits,
)


_APPROVED_HOST = "api.razorpay.com"
_APPROVED_API = "https://api.razorpay.com/v1"


class RazorpayLiveTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        ...


class RazorpayLiveHTTPTransport:
    """Fixed-origin HTTPS transport. Constructed only by the terminal canary."""

    def __init__(self, *, connection_factory=http.client.HTTPSConnection):
        self._connection_factory = connection_factory

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if not url.startswith(_APPROVED_API + "/"):
            raise RazorpayLiveProviderError(
                "RAZORPAY_URL_UNSAFE",
                "Razorpay live URL is not approved.",
                failure_class="final",
            )
        if timeout_seconds <= 0 or timeout_seconds > 10:
            raise RazorpayLiveProviderError(
                "RAZORPAY_TIMEOUT_UNSAFE",
                "Razorpay live timeout is invalid.",
                failure_class="final",
            )

        path = url.removeprefix("https://" + _APPROVED_HOST)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        safe_headers = {
            "Authorization": headers.get("Authorization", ""),
            "Content-Type": "application/json",
        }

        try:
            connection = self._connection_factory(
                _APPROVED_HOST,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            raise RazorpayLiveProviderError(
                "RAZORPAY_CONNECT_FAILED",
                "Razorpay live connection could not be established.",
                failure_class="retryable",
            ) from exc

        try:
            connection.request("POST", path, body=body, headers=safe_headers)
            response = connection.getresponse()
            response_body = response.read()
        except TimeoutError as exc:
            raise RazorpayLiveProviderError(
                "RAZORPAY_TIMEOUT",
                "Razorpay live order request timed out.",
                failure_class="unknown",
            ) from exc
        except RazorpayLiveProviderError:
            raise
        except Exception as exc:
            raise RazorpayLiveProviderError(
                "RAZORPAY_NETWORK_ERROR",
                "Razorpay live order request outcome is unknown.",
                failure_class="unknown",
            ) from exc
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

        if response.status < 200 or response.status >= 300:
            raise RazorpayLiveProviderError(
                "RAZORPAY_HTTP_ERROR",
                "Razorpay live order request returned a non-success status.",
                provider_status_code=response.status,
            )
        try:
            parsed = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RazorpayLiveProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay live order response was invalid.",
                failure_class="unknown",
            ) from exc
        if not isinstance(parsed, dict):
            raise RazorpayLiveProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay live order response was invalid.",
                failure_class="unknown",
            )
        return parsed


class RazorpayLiveOrdersClient:
    def __init__(
        self,
        *,
        config: RazorpayLiveConfig,
        transport: RazorpayLiveTransport,
    ) -> None:
        self._config = validate_razorpay_live_config(config)
        self._transport = transport

    async def create_order(
        self,
        request: RazorpayOrderCreateRequest,
    ) -> RazorpayOrderCreateResponse:
        payload = request.to_provider_payload()
        response = await self._transport.post_json(
            url=f"{self._config.api_base_url}/orders",
            headers={
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=float(self._config.timeout_seconds),
        )
        return map_razorpay_live_order_response(
            payload=response,
            expected=request,
        )

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._config.key_id}:{self._config.key_secret}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"


class RazorpayLiveCheckoutAdapter:
    provider_code = "razorpay"

    def __init__(
        self,
        *,
        config: RazorpayLiveConfig,
        client: RazorpayLiveOrdersClient,
    ) -> None:
        self._config = validate_razorpay_live_config(config)
        self._client = client

    @property
    def environment(self) -> str:
        return "live"

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        order_request = self.build_order_request(request)
        order = await self._client.create_order(order_request)
        return ProviderCheckoutIntentResponse(
            provider_code=self.provider_code,
            provider_order_ref=order.order_id,
            status=order.status,
        )

    def build_order_request(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> RazorpayOrderCreateRequest:
        amount_subunits = amount_to_razorpay_subunits(request.amount)
        if amount_subunits <= 0:
            raise RazorpayLiveProviderError(
                "RAZORPAY_ORDER_AMOUNT_INVALID",
                "Razorpay live order amount must be positive.",
                failure_class="final",
            )
        currency = request.currency_code.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise RazorpayLiveProviderError(
                "RAZORPAY_ORDER_CURRENCY_INVALID",
                "Razorpay live order currency is invalid.",
                failure_class="final",
            )
        receipt = f"fin_{request.invoice_id.hex[:32]}"
        order = RazorpayOrderCreateRequest(
            amount_subunits=amount_subunits,
            currency_code=currency,
            receipt=receipt,
            notes={
                "finance_invoice_id": str(request.invoice_id),
                "finance_idempotency_key": request.idempotency_key,
            },
        )
        joined = " ".join(
            [order.receipt, *order.notes.keys(), *order.notes.values()]
        ).lower()
        forbidden = (
            "secret",
            "token",
            "password",
            "email",
            "phone",
            self._config.key_secret.lower(),
        )
        if any(value and value in joined for value in forbidden):
            raise RazorpayLiveProviderError(
                "RAZORPAY_ORDER_NOTES_UNSAFE",
                "Razorpay live order metadata is unsafe.",
                failure_class="final",
            )
        return order

    def build_checkout_fields(
        self,
        *,
        provider_order_ref: str,
    ) -> dict[str, str]:
        return RazorpayCheckoutFields(
            key_id=self._config.key_id,
            order_id=provider_order_ref,
        ).to_browser_payload()
