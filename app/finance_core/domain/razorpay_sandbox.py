from __future__ import annotations

import hmac
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Protocol

from app.finance_core.domain.provider_boundary import FinanceProviderConfigError


RazorpaySandboxMode = Literal["sandbox", "test"]


class RazorpaySandboxClient(Protocol):
    async def create_order(self, request: RazorpayOrderCreateRequest) -> RazorpayOrderCreateResponse:
        """Injected test/sandbox client boundary. Production clients are not part of Phase 6B."""


@dataclass(frozen=True)
class RazorpaySandboxConfig:
    mode: RazorpaySandboxMode
    key_id: str
    key_secret: str
    webhook_secret: str
    merchant_reference: str
    api_base_url: str = "https://api.razorpay.com/v1"
    timeout_seconds: Decimal = Decimal("5.00")

    def redacted(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "key_id": self.key_id,
            "key_secret": "[REDACTED]",
            "webhook_secret": "[REDACTED]",
            "merchant_reference": self.merchant_reference,
            "api_base_url": self.api_base_url,
            "timeout_seconds": str(self.timeout_seconds),
        }

    def __repr__(self) -> str:
        return f"RazorpaySandboxConfig({self.redacted()!r})"


@dataclass(frozen=True)
class RazorpayOrderCreateRequest:
    amount_subunits: int
    currency_code: str
    receipt: str
    notes: dict[str, str]

    def to_provider_payload(self) -> dict[str, Any]:
        return {
            "amount": self.amount_subunits,
            "currency": self.currency_code,
            "receipt": self.receipt,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class RazorpayOrderCreateResponse:
    order_id: str
    amount_subunits: int
    currency_code: str
    receipt: str
    status: str


@dataclass(frozen=True)
class RazorpayTestModeOrderResult:
    provider_order_id: str
    amount_subunits: int
    currency_code: str
    receipt: str
    status: str
    public_key_id: str

    def to_safe_output(self) -> dict[str, str | int]:
        return {
            "provider_order_id": self.provider_order_id,
            "amount_subunits": self.amount_subunits,
            "currency_code": self.currency_code,
            "receipt": self.receipt,
            "status": self.status,
            "public_key_id": self.public_key_id,
        }


@dataclass(frozen=True)
class RazorpayProviderError(Exception):
    code: str
    message: str
    provider_status_code: int | None = None

    def __str__(self) -> str:
        status = f" status={self.provider_status_code}" if self.provider_status_code is not None else ""
        return f"{self.code}:{status} {self.message}"


@dataclass(frozen=True)
class RazorpayCheckoutFields:
    key_id: str
    order_id: str

    def to_browser_payload(self) -> dict[str, str]:
        return {"key": self.key_id, "order_id": self.order_id}


@dataclass(frozen=True)
class RazorpayCheckoutSignatureVerificationResult:
    verified: bool
    provider_order_id: str
    provider_payment_id: str


@dataclass(frozen=True)
class RazorpayCheckoutSignatureError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def validate_razorpay_sandbox_config(config: RazorpaySandboxConfig) -> RazorpaySandboxConfig:
    if config.mode not in {"sandbox", "test"}:
        raise FinanceProviderConfigError(f"Razorpay config must be sandbox/test only: {config.redacted()}")
    if not config.key_id.strip():
        raise FinanceProviderConfigError(f"Razorpay key id is required: {config.redacted()}")
    if not config.key_id.startswith("rzp_test_"):
        raise FinanceProviderConfigError(f"Razorpay sandbox key id must be a test key: {config.redacted()}")
    if not config.key_secret.strip():
        raise FinanceProviderConfigError(f"Razorpay key secret is required: {config.redacted()}")
    live_key_marker = "rzp_" + "live_"
    if live_key_marker in config.key_secret:
        raise FinanceProviderConfigError(f"Razorpay key secret must not be live-mode material: {config.redacted()}")
    if not config.webhook_secret.strip():
        raise FinanceProviderConfigError(f"Razorpay webhook secret is required: {config.redacted()}")
    if not config.merchant_reference.strip():
        raise FinanceProviderConfigError(f"Razorpay merchant reference is required: {config.redacted()}")
    if config.api_base_url != "https://api.razorpay.com/v1":
        raise FinanceProviderConfigError(f"Razorpay API base URL is not approved for test-mode adapter: {config.redacted()}")
    if config.timeout_seconds <= Decimal("0"):
        raise FinanceProviderConfigError(f"Razorpay timeout must be positive: {config.redacted()}")
    return config


def amount_to_razorpay_subunits(amount: Decimal) -> int:
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def map_razorpay_order_response(
    *,
    payload: dict[str, Any],
    expected: RazorpayOrderCreateRequest,
    public_key_id: str,
) -> RazorpayTestModeOrderResult:
    try:
        order_id = str(payload["id"])
        amount_subunits = int(payload["amount"])
        currency_code = str(payload["currency"]).upper()
        receipt = str(payload["receipt"])
        status = str(payload.get("status", "created"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RazorpayProviderError("RAZORPAY_ORDER_RESPONSE_INVALID", "Razorpay order response was invalid.") from exc

    if not order_id.startswith("order_"):
        raise RazorpayProviderError("RAZORPAY_ORDER_ID_INVALID", "Razorpay order id was invalid.")
    if amount_subunits != expected.amount_subunits:
        raise RazorpayProviderError("RAZORPAY_ORDER_AMOUNT_MISMATCH", "Razorpay order amount did not match the server invoice.")
    if currency_code != expected.currency_code.upper():
        raise RazorpayProviderError("RAZORPAY_ORDER_CURRENCY_MISMATCH", "Razorpay order currency did not match the server invoice.")
    if receipt != expected.receipt:
        raise RazorpayProviderError("RAZORPAY_ORDER_RECEIPT_MISMATCH", "Razorpay order receipt did not match the server reference.")

    return RazorpayTestModeOrderResult(
        provider_order_id=order_id,
        amount_subunits=amount_subunits,
        currency_code=currency_code,
        receipt=receipt,
        status=status,
        public_key_id=public_key_id,
    )


def verify_razorpay_webhook_signature(*, raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    expected = hmac.digest(webhook_secret.encode("utf-8"), raw_body, "sha256").hex()
    return hmac.compare_digest(expected, signature)


def verify_razorpay_checkout_signature(
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    key_secret: str,
) -> RazorpayCheckoutSignatureVerificationResult:
    order_id = _required_checkout_signature_field(
        "RAZORPAY_CHECKOUT_ORDER_ID_REQUIRED",
        "Razorpay order id is required.",
        razorpay_order_id,
    )
    payment_id = _required_checkout_signature_field(
        "RAZORPAY_CHECKOUT_PAYMENT_ID_REQUIRED",
        "Razorpay payment id is required.",
        razorpay_payment_id,
    )
    signature = _required_checkout_signature_field(
        "RAZORPAY_CHECKOUT_SIGNATURE_REQUIRED",
        "Razorpay checkout signature is required.",
        razorpay_signature,
    )
    secret = _required_checkout_signature_field(
        "RAZORPAY_CHECKOUT_KEY_SECRET_REQUIRED",
        "Razorpay key secret is required.",
        key_secret,
    )

    signed_payload = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.digest(secret.encode("utf-8"), signed_payload, "sha256").hex()
    if not hmac.compare_digest(expected, signature):
        raise RazorpayCheckoutSignatureError(
            "RAZORPAY_CHECKOUT_SIGNATURE_INVALID",
            "Razorpay checkout signature is invalid.",
        )
    return RazorpayCheckoutSignatureVerificationResult(
        verified=True,
        provider_order_id=order_id,
        provider_payment_id=payment_id,
    )


def _required_checkout_signature_field(code: str, message: str, value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise RazorpayCheckoutSignatureError(code, message)
    return normalized
