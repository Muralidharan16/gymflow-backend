from __future__ import annotations

from dataclasses import dataclass


class RazorpayWebhookError(Exception):
    pass


@dataclass(frozen=True)
class RazorpayWebhookInput:
    raw_body: bytes
    signature: str | None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RazorpayWebhookPaymentReference:
    provider_event_id: str
    event_type: str
    provider_order_id: str
    provider_payment_id: str | None
    mapped_status: str
    payload_hash: str
