"""Raw-body verification and parsing for Resend/Svix webhook events."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class VerifiedResendEvent:
    event_id: str
    provider_reference_id: str
    event_type: str
    event_created_at: datetime
    evidence_sha256: str


class ResendWebhookError(ValueError):
    pass


def _webhook_key(secret: str) -> bytes:
    value = secret.strip()
    if not value.startswith("whsec_"):
        raise ResendWebhookError("Resend webhook secret is invalid")
    try:
        decoded = base64.b64decode(value[6:], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ResendWebhookError("Resend webhook secret is invalid") from exc
    if len(decoded) < 16:
        raise ResendWebhookError("Resend webhook secret is invalid")
    return decoded


def verify_resend_webhook(
    *,
    raw_body: bytes,
    event_id: str,
    timestamp: str,
    signature_header: str,
    secret: str,
    now_epoch_seconds: int | None = None,
    tolerance_seconds: int = 300,
) -> VerifiedResendEvent:
    if not raw_body or len(raw_body) > 1_000_000:
        raise ResendWebhookError("Resend webhook body is empty or too large")
    event_id = event_id.strip()
    timestamp = timestamp.strip()
    signature_header = signature_header.strip()
    if not event_id or not timestamp or not signature_header:
        raise ResendWebhookError("Resend webhook signature headers are incomplete")
    if tolerance_seconds < 1 or tolerance_seconds > 900:
        raise ValueError("webhook tolerance must be in [1, 900]")
    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise ResendWebhookError("Resend webhook timestamp is invalid") from exc
    now = int(time.time()) if now_epoch_seconds is None else int(now_epoch_seconds)
    if abs(now - signed_at) > tolerance_seconds:
        raise ResendWebhookError("Resend webhook timestamp is outside replay tolerance")

    key = _webhook_key(secret)
    signed_content = event_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()
    supplied = []
    for token in signature_header.split():
        if "," not in token:
            continue
        version, signature = token.split(",", 1)
        if version == "v1" and signature:
            supplied.append(signature)
    if not supplied or not any(hmac.compare_digest(expected, candidate) for candidate in supplied):
        raise ResendWebhookError("Resend webhook signature is invalid")

    try:
        payload: Any = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResendWebhookError("Resend webhook JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ResendWebhookError("Resend webhook payload must be an object")
    event_type = payload.get("type")
    created_at = payload.get("created_at")
    data = payload.get("data")
    if not isinstance(event_type, str) or not isinstance(created_at, str) or not isinstance(data, dict):
        raise ResendWebhookError("Resend webhook event shape is invalid")
    reference = data.get("email_id") or data.get("id")
    if not isinstance(reference, str) or not reference.strip():
        raise ResendWebhookError("Resend webhook email reference is missing")
    try:
        parsed_created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResendWebhookError("Resend webhook event timestamp is invalid") from exc
    if parsed_created.tzinfo is None:
        raise ResendWebhookError("Resend webhook event timestamp must be timezone-aware")

    return VerifiedResendEvent(
        event_id=event_id,
        provider_reference_id=reference.strip(),
        event_type=event_type,
        event_created_at=parsed_created.astimezone(timezone.utc),
        evidence_sha256=hashlib.sha256(raw_body).hexdigest(),
    )
