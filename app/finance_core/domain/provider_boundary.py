from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class FinanceCheckoutIntentConflictError(Exception):
    pass


class FinanceCheckoutIntentStateError(Exception):
    pass


class FinanceProviderConfigError(Exception):
    pass


class FinanceWebhookSignatureError(Exception):
    pass


class FinanceWebhookNormalizationError(Exception):
    pass


@dataclass(frozen=True)
class CreateCheckoutIntentCommand:
    organization_id: uuid.UUID | None
    invoice_id: uuid.UUID
    provider_code: str
    amount: Decimal
    currency_code: str
    idempotency_key: str


@dataclass(frozen=True)
class CheckoutIntentResult:
    intent_id: uuid.UUID
    invoice_id: uuid.UUID
    status: str
    amount: Decimal
    currency_code: str
    provider_code: str
    provider_order_ref: str | None
    replayed: bool = False


@dataclass(frozen=True)
class ProviderCheckoutIntentRequest:
    invoice_id: uuid.UUID
    amount: Decimal
    currency_code: str
    idempotency_key: str


@dataclass(frozen=True)
class ProviderCheckoutIntentResponse:
    provider_code: str
    provider_order_ref: str | None
    status: str


class CheckoutIntentProvider(Protocol):
    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        """Provider boundary; production adapters are intentionally not part of Phase 5F."""


@dataclass(frozen=True)
class ProviderSandboxConfig:
    provider_code: str
    sandbox_mode: bool
    merchant_id: str
    signing_secret: str

    def redacted(self) -> dict[str, object]:
        return {
            "provider_code": self.provider_code,
            "sandbox_mode": self.sandbox_mode,
            "merchant_id": self.merchant_id,
            "signing_secret": "[REDACTED]",
        }

    def __repr__(self) -> str:
        return f"ProviderSandboxConfig({self.redacted()!r})"


@dataclass(frozen=True)
class NormalizedProviderEventCommand:
    provider_code: str
    provider_event_id: str
    event_type: str
    raw_status: str
    payload_hash: str
    signature: str
    idempotency_key: str
    payment_id: uuid.UUID | None = None


@dataclass(frozen=True)
class NormalizedProviderEventResult:
    payment_event_id: uuid.UUID
    provider_code: str
    provider_event_id: str
    event_type: str
    raw_status: str
    replayed: bool = False


class ProviderWebhookSignatureVerifier(Protocol):
    def verify(self, *, payload_hash: str, signature: str, config: ProviderSandboxConfig) -> bool:
        """Verify provider signature metadata without exposing raw secrets."""


def validate_sandbox_provider_config(config: ProviderSandboxConfig) -> ProviderSandboxConfig:
    if not config.sandbox_mode:
        raise FinanceProviderConfigError(f"Provider config must be sandbox-only: {config.redacted()}")
    if not config.provider_code or not config.provider_code.replace("_", "").isalnum() or config.provider_code != config.provider_code.lower():
        raise FinanceProviderConfigError("Provider code must be lowercase alphanumeric/underscore")
    if not config.merchant_id.strip():
        raise FinanceProviderConfigError(f"Provider merchant id is required: {config.redacted()}")
    if not config.signing_secret.strip():
        raise FinanceProviderConfigError(f"Provider signing secret is required: {config.redacted()}")
    return config


class SandboxCheckoutIntentProvider:
    def __init__(self, config: ProviderSandboxConfig):
        self.config = validate_sandbox_provider_config(config)

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        seed = f"{self.config.provider_code}:{request.invoice_id}:{request.amount}:{request.currency_code}:{request.idempotency_key}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return ProviderCheckoutIntentResponse(
            provider_code=self.config.provider_code,
            provider_order_ref=f"sandbox_order_{digest}",
            status="created",
        )


class StaticSandboxSignatureVerifier:
    def verify(self, *, payload_hash: str, signature: str, config: ProviderSandboxConfig) -> bool:
        expected = hashlib.sha256(f"{payload_hash}:{config.signing_secret}".encode("utf-8")).hexdigest()
        return signature == expected
