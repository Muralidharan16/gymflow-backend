from __future__ import annotations

import base64
import http.client
import json
from typing import Any, Protocol

from app.finance_core.domain.provider_boundary import (
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
    RazorpayRefundCreateRequest,
    RazorpayTestModeRefundResult,
    RazorpaySandboxClient,
    RazorpaySandboxConfig,
    amount_to_razorpay_subunits,
    build_razorpay_refund_receipt,
    map_razorpay_order_response,
    map_razorpay_refund_collection,
    map_razorpay_refund_response,
    validate_razorpay_payment_ref,
    validate_razorpay_refund_ref,
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


class RazorpayTestModeRefundTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        ...

    async def get_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        ...


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
            ) from exc
        try:
            connection.request("GET", path, headers=safe_headers)
            response = connection.getresponse()
            response_body = response.read()
        except TimeoutError:
            raise
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_NETWORK_ERROR",
                "Razorpay test-mode request failed safely.",
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
            )

        try:
            parsed = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RazorpayProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay test-mode response was invalid.",
            ) from exc
        if not isinstance(parsed, dict):
            raise RazorpayProviderError(
                "RAZORPAY_RESPONSE_INVALID",
                "Razorpay test-mode response was invalid.",
            )
        return parsed


class RazorpayTestModeRefundsClient:
    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        transport: RazorpayTestModeRefundTransport,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._transport = transport

    async def create_refund(
        self,
        request: RazorpayRefundCreateRequest,
    ) -> RazorpayTestModeRefundResult:
        payment_ref = validate_razorpay_payment_ref(
            request.provider_payment_ref
        )
        payload = request.to_provider_payload()
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        try:
            response_payload = await self._transport.post_json(
                url=(
                    f"{self._config.api_base_url}/payments/"
                    f"{payment_ref}/refund"
                ),
                headers=headers,
                payload=payload,
                timeout_seconds=float(self._config.timeout_seconds),
            )
        except RazorpayProviderError as exc:
            raise self._refund_error(exc, operation="create_refund") from exc
        except TimeoutError as exc:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_TIMEOUT",
                "Razorpay test-mode refund request timed out.",
                failure_class="unknown",
                operation="create_refund",
            ) from exc
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_NETWORK_ERROR",
                "Razorpay test-mode refund request failed safely.",
                failure_class="unknown",
                operation="create_refund",
            ) from exc

        return map_razorpay_refund_response(
            payload=response_payload,
            expected=request,
        )

    async def fetch_refund(
        self,
        request: RazorpayRefundCreateRequest,
        *,
        provider_refund_ref: str,
    ) -> RazorpayTestModeRefundResult:
        payment_ref = validate_razorpay_payment_ref(
            request.provider_payment_ref
        )
        refund_ref = validate_razorpay_refund_ref(provider_refund_ref)
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        try:
            response_payload = await self._transport.get_json(
                url=(
                    f"{self._config.api_base_url}/payments/"
                    f"{payment_ref}/refunds/{refund_ref}"
                ),
                headers=headers,
                timeout_seconds=float(self._config.timeout_seconds),
            )
        except RazorpayProviderError as exc:
            if exc.provider_status_code == 404:
                raise RazorpayProviderError(
                    "RAZORPAY_REFUND_NOT_FOUND",
                    "Razorpay refund could not be confirmed during reconciliation.",
                    provider_status_code=404,
                    failure_class="unknown",
                    operation="fetch_refund",
                ) from exc
            raise self._refund_error(exc, operation="fetch_refund") from exc
        except TimeoutError as exc:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_TIMEOUT",
                "Razorpay refund reconciliation timed out.",
                failure_class="unknown",
                operation="fetch_refund",
            ) from exc
        except Exception as exc:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_NETWORK_ERROR",
                "Razorpay refund reconciliation failed safely.",
                failure_class="unknown",
                operation="fetch_refund",
            ) from exc

        return map_razorpay_refund_response(
            payload=response_payload,
            expected=request,
        )

    async def discover_refund(
        self,
        request: RazorpayRefundCreateRequest,
    ) -> RazorpayTestModeRefundResult | None:
        payment_ref = validate_razorpay_payment_ref(
            request.provider_payment_ref
        )
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
        }
        found: RazorpayTestModeRefundResult | None = None
        page_size = 100
        skip = 0
        max_pages = 10

        for _page in range(max_pages):
            try:
                response_payload = await self._transport.get_json(
                    url=(
                        f"{self._config.api_base_url}/payments/"
                        f"{payment_ref}/refunds"
                        f"?count={page_size}&skip={skip}"
                    ),
                    headers=headers,
                    timeout_seconds=float(self._config.timeout_seconds),
                )
            except RazorpayProviderError as exc:
                raise self._refund_error(
                    exc,
                    operation="discover_refund",
                ) from exc
            except TimeoutError as exc:
                raise RazorpayProviderError(
                    "RAZORPAY_REFUND_TIMEOUT",
                    "Razorpay refund discovery timed out.",
                    failure_class="unknown",
                    operation="discover_refund",
                ) from exc
            except Exception as exc:
                raise RazorpayProviderError(
                    "RAZORPAY_REFUND_NETWORK_ERROR",
                    "Razorpay refund discovery failed safely.",
                    failure_class="unknown",
                    operation="discover_refund",
                ) from exc

            page_match = map_razorpay_refund_collection(
                payload=response_payload,
                expected=request,
            )
            if page_match is not None:
                if found is not None:
                    raise RazorpayProviderError(
                        "RAZORPAY_REFUND_DISCOVERY_AMBIGUOUS",
                        "Multiple Razorpay refunds matched the Finance receipt.",
                        failure_class="unknown",
                        operation="discover_refund",
                    )
                found = page_match

            count_raw = response_payload.get("count")
            if not isinstance(count_raw, int) or count_raw < 0:
                raise RazorpayProviderError(
                    "RAZORPAY_REFUND_COLLECTION_INVALID",
                    "Razorpay refund collection response was invalid.",
                    failure_class="unknown",
                    operation="discover_refund",
                )
            if count_raw < page_size:
                return found
            skip += count_raw

        raise RazorpayProviderError(
            "RAZORPAY_REFUND_DISCOVERY_OVERFLOW",
            "Razorpay refund discovery exceeded the bounded scan.",
            failure_class="unknown",
            operation="discover_refund",
        )

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._config.key_id}:{self._config.key_secret}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    @staticmethod
    def _refund_error(
        exc: RazorpayProviderError,
        *,
        operation: str,
    ) -> RazorpayProviderError:
        if exc.code == "RAZORPAY_CONNECT_FAILED":
            code = "RAZORPAY_REFUND_CONNECT_FAILED"
        elif exc.code == "RAZORPAY_HTTP_ERROR":
            code = "RAZORPAY_REFUND_HTTP_ERROR"
        elif exc.code == "RAZORPAY_NETWORK_ERROR":
            code = "RAZORPAY_REFUND_NETWORK_ERROR"
        elif exc.code == "RAZORPAY_RESPONSE_INVALID":
            code = "RAZORPAY_REFUND_RESPONSE_INVALID"
        else:
            code = exc.code
        return RazorpayProviderError(
            code,
            "Razorpay test-mode refund operation failed safely.",
            provider_status_code=exc.provider_status_code,
            failure_class=exc.failure_class,
            operation=operation,
        )


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


class RazorpaySandboxRefundAdapter:
    provider_code = "razorpay_sandbox"

    def __init__(
        self,
        *,
        config: RazorpaySandboxConfig,
        client: RazorpayTestModeRefundsClient,
        guard_service: FinanceOperationalGuardService | None = None,
    ):
        self._config = validate_razorpay_sandbox_config(config)
        self._client = client
        self._guard_service = (
            guard_service or FinanceOperationalGuardService()
        )

    @property
    def environment(self) -> str:
        return self._config.mode

    async def create_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse:
        self._guard_service.require_safe_preflight()
        provider_request = self.build_refund_request(request)
        result = await self._client.create_refund(provider_request)
        return self._provider_response(request, result)

    async def fetch_refund(
        self,
        request: ProviderRefundRequest,
        *,
        provider_refund_ref: str,
    ) -> ProviderRefundResponse:
        self._guard_service.require_safe_preflight()
        provider_request = self.build_refund_request(request)
        result = await self._client.fetch_refund(
            provider_request,
            provider_refund_ref=provider_refund_ref,
        )
        return self._provider_response(request, result)

    async def discover_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse | None:
        self._guard_service.require_safe_preflight()
        provider_request = self.build_refund_request(request)
        result = await self._client.discover_refund(provider_request)
        if result is None:
            return None
        return self._provider_response(request, result)

    def build_refund_request(
        self,
        request: ProviderRefundRequest,
    ) -> RazorpayRefundCreateRequest:
        payment_ref = validate_razorpay_payment_ref(
            request.provider_payment_ref
        )
        if request.currency_code.upper() != "INR":
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_CURRENCY_UNSUPPORTED",
                "PAY-10 Razorpay refunds support INR only.",
                failure_class="final",
                operation="create_refund",
            )
        if request.amount <= 0:
            raise RazorpayProviderError(
                "RAZORPAY_REFUND_AMOUNT_INVALID",
                "Refund amount must be positive.",
                failure_class="final",
                operation="create_refund",
            )

        receipt = build_razorpay_refund_receipt(str(request.command_id))
        provider_request = RazorpayRefundCreateRequest(
            provider_payment_ref=payment_ref,
            amount_subunits=amount_to_razorpay_subunits(request.amount),
            currency_code="INR",
            receipt=receipt,
            notes={
                "finance_refund_id": str(request.refund_id),
                "finance_refund_command_id": str(request.command_id),
            },
        )
        self._validate_safe_refund_request(provider_request)
        return provider_request

    def _validate_safe_refund_request(
        self,
        request: RazorpayRefundCreateRequest,
    ) -> None:
        joined = " ".join(
            [
                request.provider_payment_ref,
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
                failure_class="final",
                operation="create_refund",
            )

    def _provider_response(
        self,
        request: ProviderRefundRequest,
        result: RazorpayTestModeRefundResult,
    ) -> ProviderRefundResponse:
        return ProviderRefundResponse(
            provider_code=self.provider_code,
            provider_payment_ref=result.provider_payment_id,
            provider_refund_ref=result.provider_refund_id,
            status=result.status,
            amount=request.amount,
            currency_code=result.currency_code,
            receipt=result.receipt,
            provider_created_at=result.created_at,
        )
