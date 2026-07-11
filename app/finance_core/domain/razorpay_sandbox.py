from __future__ import annotations

import hmac
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Protocol

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

    def redacted(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "key_id": self.key_id,
            "key_secret": "[REDACTED]",
            "webhook_secret": "[REDACTED]",
            "merchant_reference": self.merchant_reference,
        }

    def __repr__(self) -> str:
        return f"RazorpaySandboxConfig({self.redacted()!r})"


@dataclass(frozen=True)
class RazorpayOrderCreateRequest:
    amount_subunits: int
    currency_code: str
    receipt: str
    notes: dict[str, str]


@dataclass(frozen=True)
class RazorpayOrderCreateResponse:
    order_id: str
    amount_subunits: int
    currency_code: str
    receipt: str
    status: str


@dataclass(frozen=True)
class RazorpayCheckoutFields:
    key_id: str
    order_id: str

    def to_browser_payload(self) -> dict[str, str]:
        return {"key": self.key_id, "order_id": self.order_id}


def validate_razorpay_sandbox_config(config: RazorpaySandboxConfig) -> RazorpaySandboxConfig:
    if config.mode not in {"sandbox", "test"}:
        raise FinanceProviderConfigError(f"Razorpay config must be sandbox/test only: {config.redacted()}")
    if not config.key_id.strip():
        raise FinanceProviderConfigError(f"Razorpay key id is required: {config.redacted()}")
    if not config.key_id.startswith("rzp_test_"):
        raise FinanceProviderConfigError(f"Razorpay sandbox key id must be a test key: {config.redacted()}")
    if not config.key_secret.strip():
        raise FinanceProviderConfigError(f"Razorpay key secret is required: {config.redacted()}")
    if not config.webhook_secret.strip():
        raise FinanceProviderConfigError(f"Razorpay webhook secret is required: {config.redacted()}")
    if not config.merchant_reference.strip():
        raise FinanceProviderConfigError(f"Razorpay merchant reference is required: {config.redacted()}")
    return config


def amount_to_razorpay_subunits(amount: Decimal) -> int:
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def verify_razorpay_webhook_signature(*, raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    expected = hmac.digest(webhook_secret.encode("utf-8"), raw_body, "sha256").hex()
    return hmac.compare_digest(expected, signature)
