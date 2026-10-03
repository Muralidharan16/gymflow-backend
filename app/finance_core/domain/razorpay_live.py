"""Razorpay live-order domain boundary for PAY-24-F.

This module supports order creation only. It deliberately contains no capture,
refund, webhook, payment-application, settlement, or entitlement behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from app.finance_core.domain.provider_boundary import (
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderFailureClass,
)
from app.finance_core.domain.razorpay_sandbox import (
    RazorpayOrderCreateRequest,
    RazorpayOrderCreateResponse,
    classify_razorpay_provider_failure,
)


RazorpayLiveMode = Literal["live"]
_APPROVED_API = "https://api.razorpay.com/v1"
_LIVE_KEY_PREFIX = "rzp_" + "live_"
_TEST_KEY_PREFIX = "rzp_" + "test_"
_MERCHANT_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,63}$")


@dataclass(frozen=True, slots=True)
class RazorpayLiveConfig:
    mode: RazorpayLiveMode
    key_id: str
    key_secret: str
    merchant_reference: str
    api_base_url: str = _APPROVED_API
    timeout_seconds: Decimal = Decimal("5.00")

    def redacted(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "key_id": _redact_key_id(self.key_id),
            "key_secret": "[REDACTED]",
            "merchant_reference": self.merchant_reference,
            "api_base_url": self.api_base_url,
            "timeout_seconds": str(self.timeout_seconds),
        }

    def __repr__(self) -> str:
        return f"RazorpayLiveConfig({self.redacted()!r})"


class RazorpayLiveProviderError(FinanceProviderOperationError):
    def __init__(
        self,
        code: str,
        message: str,
        provider_status_code: int | None = None,
        *,
        failure_class: ProviderFailureClass | None = None,
    ) -> None:
        super().__init__(
            provider_code="razorpay",
            operation="create_checkout",
            code=code,
            failure_class=failure_class
            or classify_razorpay_provider_failure(
                code=code,
                provider_status_code=provider_status_code,
                operation="create_checkout",
            ),
            message=message,
            provider_status_code=provider_status_code,
        )


def validate_razorpay_live_config(config: RazorpayLiveConfig) -> RazorpayLiveConfig:
    if config.mode != "live":
        raise FinanceProviderConfigError(
            f"Razorpay live config mode is invalid: {config.redacted()}"
        )
    key_id = config.key_id.strip()
    if key_id != config.key_id or not key_id.startswith(_LIVE_KEY_PREFIX):
        raise FinanceProviderConfigError(
            f"Razorpay live key id is invalid: {config.redacted()}"
        )
    if key_id.startswith(_TEST_KEY_PREFIX):
        raise FinanceProviderConfigError(
            f"Razorpay test key cannot enter live adapter: {config.redacted()}"
        )
    if not config.key_secret.strip() or config.key_secret != config.key_secret.strip():
        raise FinanceProviderConfigError(
            f"Razorpay live key secret is invalid: {config.redacted()}"
        )
    if not _MERCHANT_REFERENCE.fullmatch(config.merchant_reference):
        raise FinanceProviderConfigError(
            f"Razorpay live merchant reference is invalid: {config.redacted()}"
        )
    if config.api_base_url != _APPROVED_API:
        raise FinanceProviderConfigError(
            f"Razorpay live API origin is not approved: {config.redacted()}"
        )
    if config.timeout_seconds <= Decimal("0") or config.timeout_seconds > Decimal("10"):
        raise FinanceProviderConfigError(
            f"Razorpay live timeout is unsafe: {config.redacted()}"
        )
    return config


def map_razorpay_live_order_response(
    *,
    payload: dict[str, Any],
    expected: RazorpayOrderCreateRequest,
) -> RazorpayOrderCreateResponse:
    try:
        order_id = str(payload["id"])
        amount_subunits = int(payload["amount"])
        currency_code = str(payload["currency"]).upper()
        receipt = str(payload["receipt"])
        status = str(payload["status"]).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_RESPONSE_INVALID",
            "Razorpay live order response was invalid.",
        ) from exc

    if (
        not order_id.startswith("order_")
        or len(order_id) <= 6
        or not order_id.replace("_", "").isalnum()
    ):
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_ID_INVALID",
            "Razorpay live order id was invalid.",
        )
    if amount_subunits != expected.amount_subunits:
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_AMOUNT_MISMATCH",
            "Razorpay live order amount did not match Finance truth.",
        )
    if currency_code != expected.currency_code.upper():
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_CURRENCY_MISMATCH",
            "Razorpay live order currency did not match Finance truth.",
        )
    if receipt != expected.receipt:
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_RECEIPT_MISMATCH",
            "Razorpay live order receipt did not match Finance truth.",
        )
    if status != "created":
        raise RazorpayLiveProviderError(
            "RAZORPAY_ORDER_STATUS_INVALID",
            "Razorpay live order did not return the expected created state.",
            failure_class="unknown",
        )
    return RazorpayOrderCreateResponse(
        order_id=order_id,
        amount_subunits=amount_subunits,
        currency_code=currency_code,
        receipt=receipt,
        status=status,
    )


def _redact_key_id(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "[REDACTED]"
    return f"{value[:6]}...{value[-4:]}"
