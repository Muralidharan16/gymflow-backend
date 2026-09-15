"""Shared P8 redaction for logs, traces, error reporting and telemetry sinks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "[REDACTED]"

_EXACT_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "access_token",
    "refresh_token",
    "id_token",
    "password",
    "password_hash",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "private_key",
    "database_url",
    "auth_database_url",
    "worker_database_url",
    "maintenance_database_url",
    "redis_url",
    "celery_broker_url",
    "celery_result_backend",
    "sentry_dsn",
    "webhook_signature",
    "svix_signature",
    "provider_signature",
    "provider_secret",
    "provider_token",
    "card_number",
    "cvv",
    "cvc",
    "bank_account",
    "account_number",
    "routing_number",
    "upi_id",
    "email",
    "email_address",
    "phone",
    "phone_number",
    "mobile",
    "mobile_number",
    "government_id",
    "aadhaar",
    "aadhar",
    "pan_number",
    "passport_number",
    "ip_address",
    "address_line1",
    "address_line2",
    "postal_code",
    "coordinates",
    "lat",
    "lng",
    "latitude",
    "longitude",
    "formatted_address",
    "google_place_id",
    "maps_url",
    "embed_url",
}

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "credential",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
    "webhook_signature",
    "provider_signature",
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
_URI_CREDENTIAL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(authorization|password|passwd|secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|webhook[_-]?signature)\s*[:=]\s*([^\s,;]+)"
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_IPV4_RE = re.compile(r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)")


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def is_sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _EXACT_SENSITIVE_KEYS:
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    """Redact common secrets/PII embedded in otherwise free-form text."""

    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _URI_CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}[REDACTED]@", text)
    text = _KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _EMAIL_RE.sub(REDACTED, text)
    text = _PHONE_RE.sub(REDACTED, text)
    text = _IPV4_RE.sub(REDACTED, text)
    return text


def redact_value(value: Any, *, key: Any | None = None) -> Any:
    """Return a recursively sanitized copy without mutating the input."""

    if key is not None and is_sensitive_key(key):
        return REDACTED

    if isinstance(value, Mapping):
        return {
            str(child_key): redact_value(child_value, key=child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, set):
        return sorted((redact_value(item) for item in value), key=str)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = redact_value(value)
    if not isinstance(sanitized, dict):  # defensive: Mapping always becomes dict
        raise TypeError("redact_mapping expected a mapping")
    return sanitized
