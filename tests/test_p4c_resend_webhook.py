from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from app.services.resend_webhook import ResendWebhookError, verify_resend_webhook


SECRET_BYTES = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()
NOW = 1_800_000_000


def _body(*, event_type: str = "email.delivered", email_id: str = "email_123") -> bytes:
    return json.dumps(
        {
            "type": event_type,
            "created_at": "2026-08-16T16:30:00Z",
            "data": {"email_id": email_id},
        },
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes, *, event_id: str = "msg_123", timestamp: str | None = None) -> str:
    timestamp = timestamp or str(NOW)
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = base64.b64encode(hmac.new(SECRET_BYTES, signed, hashlib.sha256).digest()).decode()
    return f"v1,{digest}"


def test_valid_raw_body_signature_returns_provider_identity_without_tenant_input() -> None:
    body = _body()
    event = verify_resend_webhook(
        raw_body=body,
        event_id="msg_123",
        timestamp=str(NOW),
        signature_header=_signature(body),
        secret=SECRET,
        now_epoch_seconds=NOW,
    )
    assert event.event_id == "msg_123"
    assert event.provider_reference_id == "email_123"
    assert event.event_type == "email.delivered"
    assert len(event.evidence_sha256) == 64
    assert not hasattr(event, "tenant_id")


def test_mutated_body_fails_even_when_json_meaning_is_similar() -> None:
    signed_body = _body()
    mutated = signed_body + b"\n"
    with pytest.raises(ResendWebhookError, match="signature is invalid"):
        verify_resend_webhook(
            raw_body=mutated,
            event_id="msg_123",
            timestamp=str(NOW),
            signature_header=_signature(signed_body),
            secret=SECRET,
            now_epoch_seconds=NOW,
        )


def test_replayed_or_future_timestamp_outside_five_minutes_fails() -> None:
    body = _body()
    for timestamp in (NOW - 301, NOW + 301):
        with pytest.raises(ResendWebhookError, match="outside replay tolerance"):
            verify_resend_webhook(
                raw_body=body,
                event_id="msg_123",
                timestamp=str(timestamp),
                signature_header=_signature(body, timestamp=str(timestamp)),
                secret=SECRET,
                now_epoch_seconds=NOW,
            )


def test_multiple_v1_signatures_accepts_any_constant_time_match() -> None:
    body = _body()
    valid = _signature(body)
    event = verify_resend_webhook(
        raw_body=body,
        event_id="msg_123",
        timestamp=str(NOW),
        signature_header="v1,bad " + valid,
        secret=SECRET,
        now_epoch_seconds=NOW,
    )
    assert event.provider_reference_id == "email_123"


def test_bad_secret_header_and_missing_reference_fail_closed() -> None:
    body = _body(email_id="")
    with pytest.raises(ResendWebhookError, match="secret is invalid"):
        verify_resend_webhook(
            raw_body=body,
            event_id="msg_123",
            timestamp=str(NOW),
            signature_header=_signature(body),
            secret="not-a-secret",
            now_epoch_seconds=NOW,
        )
    with pytest.raises(ResendWebhookError, match="email reference is missing"):
        verify_resend_webhook(
            raw_body=body,
            event_id="msg_123",
            timestamp=str(NOW),
            signature_header=_signature(body),
            secret=SECRET,
            now_epoch_seconds=NOW,
        )


def test_body_size_and_signature_headers_are_bounded() -> None:
    with pytest.raises(ResendWebhookError, match="empty or too large"):
        verify_resend_webhook(
            raw_body=b"x" * 1_000_001,
            event_id="msg_123",
            timestamp=str(NOW),
            signature_header="v1,nope",
            secret=SECRET,
            now_epoch_seconds=NOW,
        )
    with pytest.raises(ResendWebhookError, match="headers are incomplete"):
        verify_resend_webhook(
            raw_body=_body(),
            event_id="",
            timestamp=str(NOW),
            signature_header="v1,nope",
            secret=SECRET,
            now_epoch_seconds=NOW,
        )
