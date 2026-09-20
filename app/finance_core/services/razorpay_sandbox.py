from __future__ import annotations

import base64
import hashlib
import http.client
import json
from decimal import Decimal
from typing import Any, Protocol

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
    ProviderRefundRequest,
    ProviderRefundResponse,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayCheckoutFields,
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    RazorpayProviderError,
    RazorpayRefundRequest,
    RazorpayRefundResponse,
    RazorpaySandboxClient,
    RazorpaySandboxConfig,
    RazorpaySandboxRefundClient,
    amount_to_razorpay_subunits,
    map_razorpay_order_response,
    map_razorpay_refund_response,
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

    async def get_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Injected reconciliation fetch transport."""


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


class RazorpayTestModeRefundsClient:
    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        transport: RazorpayTestModeTransport,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._transport = transport

    async def submit_refund(
        self,
        request: RazorpayRefundRequest,
    ) -> RazorpayRefundResponse:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        try:
            response_payload = await self._transport.post_json(
                url=(
                    f"{self._config.api_base_url}/payments/"
                    f"{request.provider_payment_id}/refund"
                ),
                headers=headers,
                payload=request.to_provider_payload(),
                timeout_seconds=float(self._config.timeout_seconds),
            )
        except TimeoutError as exc:
            raise RazorpayProviderError(
                "RAZORPAY_TIMEOUT",
                "Razorpay test-mode refund request timed out.",
                operation="submit_refund",
            ) from exc
        except RazorpayProviderError as exc:
            if exc.code == "RAZORPAY_HTTP_ERROR":
                raise RazorpayProviderError(
                    exc.code,
                    "Razorpay test-mode refund request returned a non-success status.",
                    provider_status_code=exc.provider_status_code,
                    operation="submit_refund",
                ) from exc
            raise
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_UNAVAILABLE",
                "Razorpay test-mode refund request failed safely.",
                operation="submit_refund",
            ) from exc
        return map_razorpay_refund_response(
            payload=response_payload,
            expected=request,
        )

    async def fetch_refund(
        self,
        request: RazorpayRefundRequest,
        *,
        provider_refund_id: str,
    ) -> RazorpayRefundResponse:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        try:
            response_payload = await self._transport.get_json(
                url=(
                    f"{self._config.api_base_url}/payments/"
                    f"{request.provider_payment_id}/refunds/{provider_refund_id}"
                ),
                headers=headers,
                timeout_seconds=float(self._config.timeout_seconds),
            )
        except TimeoutError as exc:
            raise RazorpayProviderError(
                "RAZORPAY_TIMEOUT",
                "Razorpay test-mode refund reconciliation request timed out.",
                operation="fetch_refund",
            ) from exc
        except RazorpayProviderError:
            raise
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_UNAVAILABLE",
                "Razorpay test-mode refund reconciliation request failed safely.",
                operation="fetch_refund",
            ) from exc
        return map_razorpay_refund_response(
            payload=response_payload,
            expected=request,
        )

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._config.key_id}:{self._config.key_secret}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"


class RazorpayTestModeHTTPTransport:
    """Explicit real test-mode transport. Never constructed by default routes."""

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
        if not url.startswith("https://api.razorpay.com/v1/"):
            raise RazorpayProviderError("RAZORPAY_URL_UNSAFE", "Razorpay test-mode URL is not approved.")
        if timeout_seconds <= 0:
            raise RazorpayProviderError("RAZORPAY_TIMEOUT_UNSAFE", "Razorpay test-mode timeout is invalid.")

        path = url.removeprefix("https://api.razorpay.com")
        safe_headers = {
            "Authorization": headers.get("Authorization", ""),
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        try:
            connection = self._connection_factory(
                "api.razorpay.com",
                timeout=timeout_seconds,
            )
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_CONNECT_FAILED",
                "Razorpay test-mode connection could not be established.",
            ) from exc
        try:
            connection.request("POST", path, body=body, headers=safe_headers)
            response = connection.getresponse()
            response_body = response.read()
        except TimeoutError:
            raise
        except Exception as exc:
            raise RazorpayProviderError("RAZORPAY_NETWORK_ERROR", "Razorpay test-mode request failed safely.") from exc
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

        if response.status < 200 or response.status >= 300:
            raise RazorpayProviderError(
                "RAZORPAY_HTTP_ERROR",
                "Razorpay test-mode request returned a non-success status.",
                provider_status_code=response.status,
            )

        try:
            parsed = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RazorpayProviderError("RAZORPAY_RESPONSE_INVALID", "Razorpay test-mode response was invalid.") from exc
        if not isinstance(parsed, dict):
            raise RazorpayProviderError("RAZORPAY_RESPONSE_INVALID", "Razorpay test-mode response was invalid.")
        return parsed

    async def get_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if not url.startswith("https://api.razorpay.com/v1/"):
            raise RazorpayProviderError(
                "RAZORPAY_URL_UNSAFE",
                "Razorpay test-mode URL is not approved.",
            )
        if timeout_seconds <= 0:
            raise RazorpayProviderError(
                "RAZORPAY_TIMEOUT_UNSAFE",
                "Razorpay test-mode timeout is invalid.",
            )

        path = url.removeprefix("https://api.razorpay.com")
        safe_headers = {
            "Authorization": headers.get("Authorization", ""),
            "Content-Type": "application/json",
        }
        try:
            connection = self._connection_factory(
                "api.razorpay.com",
                timeout=timeout_seconds,
            )
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_CONNECT_FAILED",
                "Razorpay test-mode connection could not be established.",
                operation="fetch_refund",
            ) from exc
        try:
            connection.request("GET", path, body=b"", headers=safe_headers)
            response = connection.getresponse()
            response_body = response.read()
        except TimeoutError:
            raise
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_NETWORK_ERROR",
                "Razorpay test-mode request failed safely.",
                operation="fetch_refund",
            ) from exc
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

        if response.status < 200 or response.status >= 300:
            raise RazorpayProviderError(
                "RAZORPAY_HTTP_ERROR",
                "Razorpay test-mode request returned a non-success status.",
                provider_status_code=response.status,
                operation="fetch_refund",
            )

        try:
            parsed = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RazorpayProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay test-mode response was invalid.",
                operation="fetch_refund",
            ) from exc
        if not isinstance(parsed, dict):
            raise RazorpayProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay test-mode response was invalid.",
                operation="fetch_refund",
            )
        return parsed


class RazorpaySandboxAdapter:
    provider_code = "razorpay_sandbox"

    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        client: RazorpaySandboxClient,
        refund_client: RazorpaySandboxRefundClient | None = None,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._client = client
        self._refund_client = refund_client
        self._guard_service = guard_service or FinanceOperationalGuardService()

    @property
    def environment(self) -> str:
        return self._config.mode

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        self._guard_service.require_safe_preflight()
        order_request = self.build_order_request(request)
        order_response = await self._client.create_order(order_request)
        if order_response.amount_subunits != order_request.amount_subunits:
            raise RazorpayProviderError(
                "RAZORPAY_ORDER_AMOUNT_MISMATCH",
                "Razorpay order amount did not match the server invoice.",
            )
        if order_response.currency_code.upper() != order_request.currency_code:
            raise RazorpayProviderError(
                "RAZORPAY_ORDER_CURRENCY_MISMATCH",
                "Razorpay order currency did not match the server invoice.",
            )
        return ProviderCheckoutIntentResponse(
            provider_code=self.provider_code,
            provider_order_ref=order_response.order_id,
            status=order_response.status,
        )

    async def submit_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse:
        self._guard_service.require_safe_preflight()
        refund_client = self._require_refund_client()
        refund_request = self.build_refund_request(request)
        refund_response = await refund_client.submit_refund(refund_request)
        return self._provider_refund_response(refund_response)

    async def fetch_refund(
        self,
        request: ProviderRefundRequest,
        *,
        provider_refund_ref: str,
    ) -> ProviderRefundResponse:
        self._guard_service.require_safe_preflight()
        self._validate_provider_refund_ref(provider_refund_ref)
        refund_client = self._require_refund_client()
        refund_request = self.build_refund_request(request)
        refund_response = await refund_client.fetch_refund(
            refund_request,
            provider_refund_id=provider_refund_ref,
        )
        if refund_response.provider_refund_id != provider_refund_ref:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_ID_INVALID",
                "Razorpay reconciliation returned a different refund id.",
                operation="fetch_refund",
            )
        return self._provider_refund_response(refund_response)

    def build_refund_request(
        self,
        request: ProviderRefundRequest,
    ) -> RazorpayRefundRequest:
        self._validate_provider_payment_ref(request.provider_payment_ref)
        if request.amount <= 0:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_AMOUNT_INVALID",
                "Refund amount must be positive.",
                operation="submit_refund",
            )
        currency_code = request.currency_code.strip().upper()
        if len(currency_code) != 3 or not currency_code.isalpha():
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_CURRENCY_INVALID",
                "Refund currency code was invalid.",
                operation="submit_refund",
            )
        amount_subunits = amount_to_razorpay_subunits(request.amount)
        if amount_subunits <= 0:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_AMOUNT_INVALID",
                "Refund amount must be at least one provider currency subunit.",
                operation="submit_refund",
            )
        receipt_seed = f"{request.command_id}:{request.refund_id}"
        receipt = (
            "rf_"
            + hashlib.sha256(receipt_seed.encode("utf-8")).hexdigest()[:32]
        )
        refund_request = RazorpayRefundRequest(
            provider_payment_id=request.provider_payment_ref,
            amount_subunits=amount_subunits,
            currency_code=currency_code,
            receipt=receipt,
            notes={
                "finance_command_id": str(request.command_id),
                "finance_refund_id": str(request.refund_id),
                "finance_payment_id": str(request.payment_id),
            },
        )
        self._validate_safe_refund_request(refund_request)
        return refund_request

    def _provider_refund_response(
        self,
        response: RazorpayRefundResponse,
    ) -> ProviderRefundResponse:
        return ProviderRefundResponse(
            provider_code=self.provider_code,
            provider_refund_ref=response.provider_refund_id,
            provider_payment_ref=response.provider_payment_id,
            amount=Decimal(response.amount_subunits) / Decimal(100),
            currency_code=response.currency_code,
            receipt=response.receipt,
            status=response.status,
        )

    def _require_refund_client(self) -> RazorpaySandboxRefundClient:
        if self._refund_client is None:
            raise FinanceProviderConfigError(
                "Razorpay refund client is not configured for PAY-10 test mode."
            )
        return self._refund_client

    @staticmethod
    def _validate_provider_payment_ref(provider_payment_ref: str) -> None:
        value = provider_payment_ref.strip()
        if (
            value != provider_payment_ref
            or not value.startswith("pay_")
            or len(value) <= 4
            or not value.replace("_", "").isalnum()
        ):
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_PAYMENT_REF_INVALID",
                "Razorpay payment reference was invalid.",
                operation="submit_refund",
            )

    @staticmethod
    def _validate_provider_refund_ref(provider_refund_ref: str) -> None:
        value = provider_refund_ref.strip()
        if (
            value != provider_refund_ref
            or not value.startswith("rfnd_")
            or len(value) <= 5
            or not value.replace("_", "").isalnum()
        ):
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_REF_INVALID",
                "Razorpay refund reference was invalid.",
                operation="fetch_refund",
            )

    def _validate_safe_refund_request(
        self,
        request: RazorpayRefundRequest,
    ) -> None:
        joined = " ".join(
            [
                request.provider_payment_id,
                request.receipt,
                *request.notes.keys(),
                *request.notes.values(),
            ]
        ).lower()
        live_key_marker = "rzp_" + "live_"
        forbidden = (
            "secret",
            "token",
            "password",
            "email",
            "phone",
            live_key_marker,
            self._config.key_secret.lower(),
            self._config.webhook_secret.lower(),
        )
        if any(value and value in joined for value in forbidden):
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_NOTES_UNSAFE",
                "Razorpay refund metadata contained unsafe fields.",
                operation="submit_refund",
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

    def build_checkout_fields(
        self,
        *,
        provider_order_ref: str,
    ) -> dict[str, str]:
        return self.checkout_fields(
            order_id=provider_order_ref,
        ).to_browser_payload()

    def checkout_fields(self, *, order_id: str) -> RazorpayCheckoutFields:
        return RazorpayCheckoutFields(key_id=self._config.key_id, order_id=order_id)

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        return verify_razorpay_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            webhook_secret=self._config.webhook_secret,
        )
