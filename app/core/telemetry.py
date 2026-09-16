from __future__ import annotations

from typing import Any, Dict

from app.observability.redaction import redact_mapping


# Historical safe-event behavior omitted these top-level location values entirely.
# Retaining the omission keeps the existing analytics contract while the shared P8
# redactor additionally protects nested secrets and other sensitive values.
_OMITTED_SAFE_EVENT_KEYS = {
    "address_line1",
    "address_line2",
    "postal_code",
    "coordinates",
    "lat",
    "lng",
    "formatted_address",
    "ip_address",
    "google_place_id",
    "latitude",
    "longitude",
    "maps_url",
    "embed_url",
}


def track_event(event_name: str, properties: Dict[str, Any]) -> None:
    """Telemetry sink adapter; concrete analytics delivery remains optional."""

    pass


def track_event_safe(event_name: str, properties: Dict[str, Any]) -> None:
    """Send only a recursively redacted, non-mutating telemetry payload."""

    safe_props = redact_mapping(properties)
    for key in _OMITTED_SAFE_EVENT_KEYS:
        safe_props.pop(key, None)
    track_event(event_name, safe_props)


def sentry_before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Dict[str, Any]:
    """Redact the complete Sentry event before the SDK sends it off-process."""

    del hint  # Sentry hint can contain raw exception objects; never serialize it here.
    return redact_mapping(event)
