"""PAY-3 canonical monetary-command protocol.

Pure domain helpers only. No database connection and no provider calls.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$")
_SAFE_SCOPE = re.compile(r"^[a-z][a-z0-9_.]{2,119}$")
_SAFE_ACTOR = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MonetaryCommandError(ValueError):
    pass


class MonetaryCommandStatus(str, Enum):
    PROCESSING = "processing"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED_DETERMINISTIC = "failed_deterministic"


@dataclass(frozen=True)
class MonetaryCommandIdentity:
    organization_id: uuid.UUID
    scope: str
    idempotency_key: str
    request_hash_sha256: str
    business_reference: str
    correlation_id: uuid.UUID
    actor_type: str
    actor_ref_sha256: str

    def __post_init__(self) -> None:
        if not _SAFE_SCOPE.fullmatch(self.scope):
            raise MonetaryCommandError("Invalid monetary command scope")
        if not _SAFE_KEY.fullmatch(self.idempotency_key):
            raise MonetaryCommandError("Invalid monetary command idempotency key")
        if not _SHA256.fullmatch(self.request_hash_sha256):
            raise MonetaryCommandError("Invalid monetary command request hash")
        if not _SAFE_KEY.fullmatch(self.business_reference):
            raise MonetaryCommandError("Invalid monetary command business reference")
        if not _SAFE_ACTOR.fullmatch(self.actor_type):
            raise MonetaryCommandError("Invalid monetary command actor type")
        if not _SHA256.fullmatch(self.actor_ref_sha256):
            raise MonetaryCommandError("Invalid monetary command actor reference hash")


def validate_response_ref(value: str) -> str:
    if not _SAFE_KEY.fullmatch(value):
        raise MonetaryCommandError("Invalid monetary command response reference")
    return value


def validate_error_code(value: str) -> str:
    if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", value):
        raise MonetaryCommandError("Invalid monetary command error code")
    if any(token in value for token in ("secret", "token", "bearer")):
        raise MonetaryCommandError("Sensitive material is forbidden in monetary error codes")
    return value


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise MonetaryCommandError("Non-finite Decimal is forbidden")
    normalized = value.normalize()
    text_value = format(normalized, "f")
    if text_value in {"-0", "-0.0"}:
        return "0"
    return text_value


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise MonetaryCommandError("float is forbidden in canonical monetary payloads")
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MonetaryCommandError("naive datetime is forbidden")
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MonetaryCommandError("monetary payload object keys must be strings")
            normalized[key] = _canonicalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    raise MonetaryCommandError(f"Unsupported monetary payload type: {type(value).__name__}")


def canonical_monetary_request_hash(payload: Mapping[str, Any]) -> str:
    canonical = _canonicalize(payload)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def actor_reference_hash(actor_reference: str) -> str:
    if not isinstance(actor_reference, str) or not actor_reference:
        raise MonetaryCommandError("actor reference is required")
    return hashlib.sha256(actor_reference.encode("utf-8")).hexdigest()


TERMINAL_STATUSES = frozenset({
    MonetaryCommandStatus.SUCCEEDED,
    MonetaryCommandStatus.FAILED_DETERMINISTIC,
})

PAY3_INVARIANTS = frozenset({
    "same_tenant_scope_key_same_payload_replays_original_command",
    "same_tenant_scope_key_different_payload_conflicts",
    "logical_command_unknown_blocks_replacement_effect_until_reconciled",
    "terminal_command_truth_is_immutable",
    "command_evidence_has_no_automatic_expiry",
    "actor_reference_is_persisted_only_as_sha256",
    "business_payload_hashing_rejects_float",
    "runtime_has_no_direct_monetary_command_table_dml",
    "live_money_movement_remains_disabled",
})
