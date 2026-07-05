from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


WEBHOOK_PAYLOAD_HASH_ALGORITHM = "sha256"

WEBHOOK_SUPPORTED_EVENT_TYPES = frozenset(
    {
        "provider_operation.succeeded",
        "provider_operation.failed",
        "provider_operation.unknown",
    }
)


@dataclass(frozen=True)
class WebhookTransportHeaders:
    values: Mapping[str, str]

    def get(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.values.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class WebhookEnvelope:
    provider_code: str
    raw_body: bytes
    headers: WebhookTransportHeaders
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    verification_context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifiedWebhook:
    provider_code: str
    provider_event_id: str
    normalized_event_type: str
    provider_timestamp: datetime
    external_customer_ref: str | None = None
    external_operation_ref: str | None = None
    external_object_ref: str | None = None
    organization_id_hint: uuid.UUID | None = None
    safe_evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredWebhookPayload:
    encrypted_payload_ref: str


@dataclass(frozen=True)
class WebhookInboxSnapshot:
    id: uuid.UUID
    provider_code: str
    provider_event_id: str
    payload_sha256: str
    encrypted_payload_ref: str
    normalized_event_type: str
    processing_status: str
    attempt_count: int
    received_at: datetime
    processed_at: datetime | None
    error_classification: str | None
    error_detail_safe: str | None
    created_at: datetime
    updated_at: datetime
    was_created: bool = False
    duplicate_replay: bool = False
    processing_claimed: bool = False


@dataclass(frozen=True)
class WebhookProcessingClaim:
    inbox: WebhookInboxSnapshot
    attempt_number: int
    claimed_at: datetime
    claimed: bool

    @property
    def inbox_id(self) -> uuid.UUID:
        return self.inbox.id


@dataclass(frozen=True)
class WebhookAcceptanceResult:
    inbox: WebhookInboxSnapshot
    accepted: bool
    duplicate_replay: bool


@dataclass(frozen=True)
class NormalizedWebhookEvent:
    provider_code: str
    provider_event_id: str
    normalized_event_type: str
    payload_sha256: str
    encrypted_payload_ref: str
    external_customer_ref: str | None
    external_operation_ref: str | None
    external_object_ref: str | None
    organization_id_hint: uuid.UUID | None = None


@dataclass(frozen=True)
class WebhookProcessingResult:
    inbox: WebhookInboxSnapshot
    status: str
    provider_operation_id: uuid.UUID | None = None
    error_classification: str | None = None


class WebhookError(Exception):
    """Base class for provider-neutral webhook domain errors."""


class WebhookVerificationError(WebhookError):
    pass


class WebhookSignatureInvalid(WebhookVerificationError):
    pass


class WebhookSignatureMissing(WebhookVerificationError):
    pass


class WebhookTimestampInvalid(WebhookVerificationError):
    pass


class WebhookPayloadStorageFailure(WebhookError):
    pass


class WebhookDuplicateConflict(WebhookError):
    pass


class WebhookInboxAcceptanceFailure(WebhookError):
    pass


class WebhookClaimConflict(WebhookError):
    pass


class WebhookClaimLost(WebhookError):
    pass


class WebhookUnsupportedEvent(WebhookError):
    pass


class WebhookUnknownMapping(WebhookError):
    pass


class WebhookEvidenceConflict(WebhookError):
    pass


class WebhookProcessingFailure(WebhookError):
    pass


def compute_webhook_payload_sha256(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
