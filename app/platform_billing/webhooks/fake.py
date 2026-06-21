from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime, timezone

from app.platform_billing.domain.webhooks import (
    VerifiedWebhook,
    WebhookEnvelope,
    WebhookSignatureInvalid,
    WebhookSignatureMissing,
    WebhookTimestampInvalid,
)


FAKE_WEBHOOK_SIGNATURE_HEADER = "x-fake-signature"
FAKE_WEBHOOK_TIMESTAMP_HEADER = "x-fake-timestamp"


class DeterministicFakeWebhookVerifier:
    def __init__(
        self,
        *,
        secret: bytes = b"phase4c-deterministic-fake-secret",
        tolerance_seconds: int = 300,
        now: datetime | None = None,
    ):
        self._secret = secret
        self._tolerance_seconds = tolerance_seconds
        self._now = now

    async def verify(self, envelope: WebhookEnvelope) -> VerifiedWebhook:
        timestamp_header = envelope.headers.get(FAKE_WEBHOOK_TIMESTAMP_HEADER)
        signature_header = envelope.headers.get(FAKE_WEBHOOK_SIGNATURE_HEADER)
        if not timestamp_header:
            raise WebhookSignatureMissing("Missing fake webhook timestamp")
        if not signature_header:
            raise WebhookSignatureMissing("Missing fake webhook signature")

        try:
            timestamp = int(timestamp_header)
        except ValueError as exc:
            raise WebhookTimestampInvalid("Malformed fake webhook timestamp") from exc

        now = self._now or datetime.now(timezone.utc)
        if abs(int(now.timestamp()) - timestamp) > self._tolerance_seconds:
            raise WebhookTimestampInvalid("Stale fake webhook timestamp")

        expected = sign_fake_webhook(
            raw_body=envelope.raw_body,
            timestamp=timestamp,
            secret=self._secret,
        )
        if not signature_header.startswith("v1="):
            raise WebhookSignatureInvalid("Malformed fake webhook signature")
        if not hmac.compare_digest(signature_header, expected):
            raise WebhookSignatureInvalid("Invalid fake webhook signature")

        try:
            payload = json.loads(envelope.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookSignatureInvalid("Malformed verified fake webhook payload") from exc

        event_id = _nonempty_string(payload.get("id"), "Missing fake webhook event id")
        event_type = _nonempty_string(payload.get("type"), "Missing fake webhook event type")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        organization_id_hint = None
        if isinstance(data.get("organization_id"), str):
            try:
                organization_id_hint = uuid.UUID(data["organization_id"])
            except ValueError as exc:
                raise WebhookSignatureInvalid("Malformed fake webhook organization id") from exc
        return VerifiedWebhook(
            provider_code=envelope.provider_code,
            provider_event_id=event_id,
            normalized_event_type=event_type,
            provider_timestamp=datetime.fromtimestamp(timestamp, tz=timezone.utc),
            external_customer_ref=_optional_string(data.get("external_customer_ref")),
            external_operation_ref=_optional_string(data.get("external_operation_ref")),
            external_object_ref=_optional_string(data.get("external_object_ref")),
            organization_id_hint=organization_id_hint,
            safe_evidence={
                "provider_event_id": event_id,
                "normalized_event_type": event_type,
            },
        )


def sign_fake_webhook(
    *,
    raw_body: bytes,
    timestamp: int,
    secret: bytes = b"phase4c-deterministic-fake-secret",
) -> str:
    signed_payload = str(timestamp).encode("ascii") + b"." + raw_body
    digest = hmac.new(secret, signed_payload, "sha256").hexdigest()
    return f"v1={digest}"


def _nonempty_string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebhookSignatureInvalid(message)
    return value


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
