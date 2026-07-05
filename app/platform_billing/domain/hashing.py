"""
app/platform_billing/domain/hashing.py
=======================================
Deterministic canonical input hashing for entitlement and access resolvers.

Semantically identical inputs always produce the same SHA-256 digest.
Meaningful changes produce different digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# Resolver version identifiers — defined once, used across all modules.
ENTITLEMENT_RESOLVER_VERSION = "entitlement-resolver-v1"
ACCESS_RESOLVER_VERSION = "access-resolver-v1"
CANONICAL_SERIALIZER_VERSION = "canonical-input-v1"


class CanonicalSerializer:
    """
    Deterministic JSON serializer for resolver inputs.

    Rules:
        - Sorted keys at every nesting level.
        - Enums serialized by their .value.
        - datetimes serialized as UTC ISO 8601 with 'Z' suffix.
        - UUIDs serialized as hex strings (no dashes or {}).
        - tuples/lists serialized as JSON arrays.
        - None/null values omitted.
    """

    @staticmethod
    def serialize(obj: Any) -> str:
        return json.dumps(
            CanonicalSerializer._prepare(obj),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _prepare(obj: Any) -> Any:
        if obj is None:
            return None
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            # Always UTC, remove microseconds, add Z suffix
            dt_utc = obj.astimezone(timezone.utc).replace(microsecond=0)
            return dt_utc.isoformat().replace("+00:00", "Z")
        if isinstance(obj, dict):
            return {str(k): CanonicalSerializer._prepare(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, (list, tuple)):
            return [CanonicalSerializer._prepare(v) for v in obj]
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, int):
            return obj
        if isinstance(obj, float):
            return obj
        return str(obj)


def compute_input_hash(
    resolver_version: str,
    input_dto: Any,
) -> str:
    """Compute SHA-256 digest of resolver inputs."""
    canonical = CanonicalSerializer.serialize(input_dto)
    payload = f"{resolver_version}:{CANONICAL_SERIALIZER_VERSION}:{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()