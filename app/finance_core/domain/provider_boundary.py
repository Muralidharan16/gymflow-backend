from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


class FinanceCheckoutIntentConflictError(Exception):
    pass


class FinanceCheckoutIntentStateError(Exception):
    pass


class FinanceProviderConfigError(Exception):
    pass


ProviderEnvironment = Literal["sandbox", "test"]
ProviderFailureClass = Literal["retryable", "final", "unknown"]


class FinanceProviderOperationError(Exception):
    """Provider-neutral outbound operation failure.

    The message must be safe for logs/API translation. Raw provider bodies,
    credentials, authorization headers and secret material never belong here.
    """

    def __init__(
        self,
        *,
        provider_code: str,
        operation: str,
        code: str,
        failure_class: ProviderFailureClass,
        message: str,
        provider_status_code: int | None = None,
    ):
        self.provider_code = provider_code
        self.operation = operation
        self.code = code
        self.failure_class = failure_class
        self.message = message
        self.provider_status_code = provider_status_code
        super().__init__(message)

    @property
    def automatic_retry_allowed(self) -> bool:
        return self.failure_class == "retryable"

    @property
    def requires_reconciliation(self) -> bool:
        return self.failure_class == "unknown"

    def __str__(self) -> str:
        status = (
            f" status={self.provider_status_code}"
            if self.provider_status_code is not None
            else ""
        )
        return (
            f"{self.provider_code}:{self.operation}:{self.code}:"
            f"{self.failure_class}{status} {self.message}"
        )


class FinanceWebhookSignatureError(Exception):
    pass


class FinanceWebhookNormalizationError(Exception):
    pass


class FinancePaymentStateTransitionError(Exception):
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


ProviderRefundStatus = Literal["pending", "processed", "failed"]


@dataclass(frozen=True)
class ProviderRefundRequest:
    """Server-authoritative provider refund request.

    Every field is loaded from durable Finance truth by later PAY-10
    capabilities. The adapter never accepts browser-selected payment, amount,
    currency, or provider references.
    """

    command_id: uuid.UUID
    refund_id: uuid.UUID
    payment_id: uuid.UUID
    provider_payment_ref: str
    amount: Decimal
    currency_code: str


@dataclass(frozen=True)
class ProviderRefundResponse:
    provider_code: str
    provider_refund_ref: str
    provider_payment_ref: str
    amount: Decimal
    currency_code: str
    receipt: str
    status: ProviderRefundStatus


class RefundProvider(Protocol):
    @property
    def provider_code(self) -> str:
        ...

    @property
    def environment(self) -> ProviderEnvironment:
        ...

    async def submit_refund(
        self,
        request: ProviderRefundRequest,
    ) -> ProviderRefundResponse:
        """Submit one logical refund without mutating Finance truth."""

    async def fetch_refund(
        self,
        request: ProviderRefundRequest,
        *,
        provider_refund_ref: str,
    ) -> ProviderRefundResponse:
        """Reconcile a known provider refund without creating a new refund."""


class CheckoutIntentProvider(Protocol):
    @property
    def provider_code(self) -> str:
        ...

    @property
    def environment(self) -> ProviderEnvironment:
        ...

    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        """Create a provider checkout object without mutating Finance truth."""

    def build_checkout_fields(self, *, provider_order_ref: str) -> dict[str, str]:
        """Return the minimal browser-safe provider fields."""


class CheckoutProviderRegistry:
    """Server-owned registry. Provider choice is never browser authority."""

    def __init__(self, adapters: tuple[CheckoutIntentProvider, ...]):
        if not adapters:
            raise FinanceProviderConfigError("At least one checkout provider adapter is required.")
        mapping: dict[str, CheckoutIntentProvider] = {}
        for adapter in adapters:
            code = adapter.provider_code
            if (
                not code
                or code != code.lower()
                or not code.replace("_", "").isalnum()
            ):
                raise FinanceProviderConfigError("Checkout provider code is invalid.")
            if adapter.environment not in {"sandbox", "test"}:
                raise FinanceProviderConfigError(
                    "PAY-7 registry accepts sandbox/test adapters only."
                )
            if code in mapping:
                raise FinanceProviderConfigError(
                    f"Duplicate checkout provider adapter: {code}"
                )
            mapping[code] = adapter
        self._adapters = mapping

    def resolve(self, provider_code: str) -> CheckoutIntentProvider:
        try:
            return self._adapters[provider_code]
        except KeyError as exc:
            raise FinanceProviderConfigError(
                f"Checkout provider is not registered: {provider_code}"
            ) from exc

    @property
    def provider_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


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
    payment_id: uuid.UUID | None = None
    payment_status: str | None = None
    state_applied: bool = False
    state_ignored: bool = False
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

    @property
    def provider_code(self) -> str:
        return self.config.provider_code

    @property
    def environment(self) -> ProviderEnvironment:
        return "sandbox"

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

    def build_checkout_fields(
        self,
        *,
        provider_order_ref: str,
    ) -> dict[str, str]:
        return {"order_id": provider_order_ref}


class StaticSandboxSignatureVerifier:
    def verify(self, *, payload_hash: str, signature: str, config: ProviderSandboxConfig) -> bool:
        expected = hashlib.sha256(f"{payload_hash}:{config.signing_secret}".encode("utf-8")).hexdigest()
        return signature == expected


ALLOWED_PAYMENT_STATE_TRANSITIONS = {
    "created": {"pending", "authorized", "captured", "failed", "cancelled"},
    "pending": {"authorized", "captured", "failed", "cancelled"},
    "authorized": {"captured", "failed", "cancelled"},
    "captured": {"settled", "partially_refunded", "refunded"},
    "settled": {"partially_refunded", "refunded"},
    "partially_refunded": {"refunded"},
    "failed": set(),
    "cancelled": set(),
    "refunded": set(),
}

PAYMENT_STATE_ORDER = {
    "created": 0,
    "pending": 1,
    "authorized": 2,
    "captured": 3,
    "settled": 4,
    "partially_refunded": 5,
    "refunded": 6,
}


def payment_state_transition_action(current_status: str, target_status: str) -> str:
    if current_status == target_status:
        return "noop"
    if current_status in {"failed", "cancelled", "refunded"}:
        raise FinancePaymentStateTransitionError(f"Invalid payment transition: {current_status} -> {target_status}")
    if target_status in ALLOWED_PAYMENT_STATE_TRANSITIONS.get(current_status, set()):
        return "apply"
    if (
        current_status in PAYMENT_STATE_ORDER
        and target_status in PAYMENT_STATE_ORDER
        and PAYMENT_STATE_ORDER[target_status] < PAYMENT_STATE_ORDER[current_status]
    ):
        return "ignore_stale"
    raise FinancePaymentStateTransitionError(f"Invalid payment transition: {current_status} -> {target_status}")
