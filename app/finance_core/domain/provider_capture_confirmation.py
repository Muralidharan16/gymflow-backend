from __future__ import annotations

import uuid
from dataclasses import dataclass


VERIFIED_RAZORPAY_WEBHOOK_SOURCE = "razorpay_webhook_verified"
SUPPORTED_CAPTURE_CONFIRMATION_EVENTS = {
    "payment.authorized",
    "payment.captured",
    "payment.failed",
    "order.paid",
}


@dataclass(frozen=True)
class ConfirmProviderPaymentEvidenceCommand:
    provider_code: str
    provider_event_id: str
    event_type: str
    provider_order_ref: str
    provider_payment_ref: str
    provider_amount_subunits: int
    provider_currency: str
    provider_payment_status: str
    provider_captured: bool | None
    provider_payment_order_ref: str
    idempotency_key: str
    webhook_signature_verified: bool
    source: str = VERIFIED_RAZORPAY_WEBHOOK_SOURCE
    provider_order_entity_ref: str | None = None
    provider_order_status: str | None = None
    provider_event_timestamp: int | None = None


@dataclass(frozen=True)
class ProviderPaymentEvidenceResult:
    payment_event_id: uuid.UUID
    payment_id: uuid.UUID
    provider_code: str
    provider_event_id: str
    event_type: str
    previous_payment_status: str
    payment_status: str
    event_recorded: bool
    state_changed: bool
    state_ignored: bool
    replayed: bool


@dataclass(frozen=True)
class FinanceProviderEvidenceError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
