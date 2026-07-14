from __future__ import annotations

import uuid
from dataclasses import dataclass


CHECKOUT_CALLBACK_EVENT_TYPE = "razorpay.checkout.callback.verified"
CHECKOUT_CALLBACK_SOURCE = "razorpay_checkout_callback"


@dataclass(frozen=True)
class RecordCheckoutCallbackCommand:
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    idempotency_key: str
    source: str = CHECKOUT_CALLBACK_SOURCE


@dataclass(frozen=True)
class CheckoutCallbackRecordingResult:
    payment_id: uuid.UUID
    provider_order_ref: str
    provider_payment_ref: str
    previous_payment_status: str
    payment_status: str
    verification_result: str
    event_recorded: bool
    replayed: bool


@dataclass(frozen=True)
class FinanceCheckoutCallbackError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def redact_provider_reference(value: str) -> str:
    suffix = value[-6:] if len(value) > 6 else ""
    return f"[REDACTED]...{suffix}" if suffix else "[REDACTED]"
