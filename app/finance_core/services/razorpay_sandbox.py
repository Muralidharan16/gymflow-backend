from __future__ import annotations

import base64
from typing import Any, Protocol

from app.finance_core.domain.provider_boundary import (
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutFields,
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    RazorpayProviderError,
    RazorpaySandboxClient,
    RazorpaySandboxConfig,
    amount_to_razorpay_subunits,
    map_razorpay_order_response,
    validate_razorpay_sandbox_config,
    verify_razorpay_webhook_signature,
)
from app.finance_core.services.operational_guards import FinanceOperationalGuardService


class RazorpayTestModeTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Injected transport. No default network client is constructed in Finance Core."""


class RazorpayTestModeOrdersClient:
    def __init__(self, *, config: RazorpaySandboxConfig, transport: RazorpayTestModeTransport):
        self._config = validate_razorpay_sandbox_config(config)
        self._transport = transport

    async def create_order(self, request: RazorpayOrderCreateRequest):
        payload = request.to_provider_payload()
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        try:
            response_payload = await self._transport.post_json(
                url=f"{self._config.api_base_url}/orders",
                headers=headers,
                payload=payload,
                timeout_seconds=float(self._config.timeout_seconds),
            )
        except TimeoutError as exc:
            raise RazorpayProviderError("RAZORPAY_TIMEOUT", "Razorpay test-mode order request timed out.") from exc
        except RazorpayProviderError:
            raise
        except Exception as exc:
            raise RazorpayProviderError("RAZORPAY_UNAVAILABLE", "Razorpay test-mode order request failed safely.") from exc

        result = map_razorpay_order_response(
            payload=response_payload,
            expected=request,
            public_key_id=self._config.key_id,
        )
        return RazorpayOrderCreateResponse(
            order_id=result.provider_order_id,
            amount_subunits=result.amount_subunits,
            currency_code=result.currency_code,
            receipt=result.receipt,
            status=result.status,
        )

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(f"{self._config.key_id}:{self._config.key_secret}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"


class RazorpaySandboxAdapter:
    provider_code = "razorpay_sandbox"

    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        client: RazorpaySandboxClient,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._client = client
        self._guard_service = guard_service or FinanceOperationalGuardService()

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        self._guard_service.require_safe_preflight()
        order_request = self.build_order_request(request)
        order_response = await self._client.create_order(order_request)
        if order_response.amount_subunits != order_request.amount_subunits:
            raise ValueError("Razorpay order response amount mismatch")
        if order_response.currency_code.upper() != order_request.currency_code:
            raise ValueError("Razorpay order response currency mismatch")
        return ProviderCheckoutIntentResponse(
            provider_code=self.provider_code,
            provider_order_ref=order_response.order_id,
            status=order_response.status,
        )

    def build_order_request(self, request: ProviderCheckoutIntentRequest) -> RazorpayOrderCreateRequest:
        amount_subunits = amount_to_razorpay_subunits(request.amount)
        currency_code = request.currency_code.upper()
        receipt = f"fin_{request.invoice_id.hex[:32]}"
        order_request = RazorpayOrderCreateRequest(
            amount_subunits=amount_subunits,
            currency_code=currency_code,
            receipt=receipt,
            notes={
                "finance_invoice_id": str(request.invoice_id),
                "finance_idempotency_key": request.idempotency_key,
            },
        )
        self._validate_safe_order_request(order_request)
        return order_request

    def _validate_safe_order_request(self, request: RazorpayOrderCreateRequest) -> None:
        joined = " ".join([request.receipt, *request.notes.keys(), *request.notes.values()]).lower()
        live_key_marker = "rzp_" + "live_"
        forbidden = ("secret", "token", "password", "email", "phone", live_key_marker, self._config.key_secret.lower(), self._config.webhook_secret.lower())
        if any(value and value in joined for value in forbidden):
            raise RazorpayProviderError("RAZORPAY_ORDER_NOTES_UNSAFE", "Razorpay order metadata contained unsafe fields.")

    def checkout_fields(self, *, order_id: str) -> RazorpayCheckoutFields:
        return RazorpayCheckoutFields(key_id=self._config.key_id, order_id=order_id)

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        return verify_razorpay_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            webhook_secret=self._config.webhook_secret,
        )
