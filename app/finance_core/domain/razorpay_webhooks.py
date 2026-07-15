from __future__ import annotations

from dataclasses import dataclass


class RazorpayWebhookError(Exception):
    pass


@dataclass(frozen=True)
class RazorpayWebhookInput:
    raw_body: bytes
    signature: str | None
    provider_event_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RazorpayWebhookPaymentReference:
    provider_event_id: str
    event_type: str
    provider_order_id: str
    provider_payment_id: str
    provider_amount_subunits: int
    provider_currency: str
    provider_payment_status: str
    provider_captured: bool | None
    provider_payment_order_id: str
    provider_order_entity_id: str | None
    provider_order_status: str | None
    provider_event_timestamp: int | None
    mapped_status: str
    signature_verified: bool
