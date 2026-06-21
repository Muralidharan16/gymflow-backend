from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from app.platform_billing.domain.hashing import CanonicalSerializer


RECONCILIATION_IDENTITY_VERSION = "platform-reconciliation-run-v1"
RECONCILIATION_EVIDENCE_VERSION = "platform-reconciliation-evidence-v1"

PROVIDER_TERMINAL_SUCCEEDED = "succeeded"
PROVIDER_TERMINAL_FAILED = "failed"
PROVIDER_PENDING = "pending"
PROVIDER_NOT_FOUND = "not_found"
PROVIDER_AMBIGUOUS = "ambiguous"

SUPPORTED_PROVIDER_EVIDENCE_STATUSES = frozenset(
    {
        PROVIDER_TERMINAL_SUCCEEDED,
        PROVIDER_TERMINAL_FAILED,
        PROVIDER_PENDING,
        PROVIDER_NOT_FOUND,
        PROVIDER_AMBIGUOUS,
    }
)


@dataclass(frozen=True)
class ReconciliationRunRequest:
    provider_code: str
    scope: Mapping[str, Any] = field(default_factory=dict)
    watermark: Mapping[str, Any] = field(default_factory=dict)
    organization_id: uuid.UUID | None = None
    object_class: str = "provider_operation"


@dataclass(frozen=True)
class ProviderOperationEvidence:
    provider_code: str
    external_operation_ref: str
    provider_status: str
    observed_at: datetime | None
    evidence_ref: str
    evidence_sha256: str
    safe_evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationPage:
    evidence: tuple[ProviderOperationEvidence, ...]
    next_watermark: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationRunSnapshot:
    id: uuid.UUID
    provider_code: str
    run_identity: str
    status: str
    claim_state: str
    scope_json: dict[str, Any]
    watermark_json: dict[str, Any]
    attempt_count: int
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    last_error_code: str | None
    last_error_at: datetime | None
    scanned_count: int
    discrepancy_count: int
    resolved_count: int
    failed_count: int
    created_at: datetime
    updated_at: datetime
    was_created: bool = False
    claimed: bool = False


@dataclass(frozen=True)
class ReconciliationItemSnapshot:
    id: uuid.UUID
    reconciliation_run_id: uuid.UUID
    organization_id: uuid.UUID | None
    provider_object_type: str
    external_object_ref: str
    local_object_type: str | None
    local_object_id: uuid.UUID | None
    discrepancy_classification: str
    resolution_status: str
    claim_state: str
    attempt_count: int
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    evidence_sha256: str
    evidence_ref: str
    last_error_code: str | None
    last_error_at: datetime | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    was_created: bool = False
    claimed: bool = False


@dataclass(frozen=True)
class ReconciliationRunClaim:
    run: ReconciliationRunSnapshot
    attempt_number: int
    claimed_at: datetime
    claim_expires_at: datetime
    claimed: bool

    @property
    def run_id(self) -> uuid.UUID:
        return self.run.id


@dataclass(frozen=True)
class ReconciliationItemClaim:
    item: ReconciliationItemSnapshot
    attempt_number: int
    claimed_at: datetime
    claim_expires_at: datetime
    claimed: bool

    @property
    def item_id(self) -> uuid.UUID:
        return self.item.id


@dataclass(frozen=True)
class ReconciliationRunResult:
    run: ReconciliationRunSnapshot
    discovered: int
    processed: int
    resolved: int
    failed: int


@dataclass(frozen=True)
class ReconciliationItemResult:
    item: ReconciliationItemSnapshot
    status: str
    classification: str
    provider_operation_id: uuid.UUID | None = None


class ReconciliationError(Exception):
    """Base class for provider-neutral reconciliation errors."""


class ReconciliationRunConflict(ReconciliationError):
    pass


class ReconciliationRunNotFound(ReconciliationError):
    pass


class ReconciliationRunClaimLost(ReconciliationError):
    pass


class ReconciliationItemNotFound(ReconciliationError):
    pass


class ReconciliationClaimLost(ReconciliationError):
    pass


class ReconciliationEvidenceStale(ReconciliationError):
    pass


class ReconciliationEvidenceAmbiguous(ReconciliationError):
    pass


class ReconciliationEvidenceConflict(ReconciliationError):
    pass


class ReconciliationUnknownMapping(ReconciliationError):
    pass


class ReconciliationProviderFailure(ReconciliationError):
    pass


class ReconciliationTransitionRejected(ReconciliationError):
    pass


def compute_run_identity(request: ReconciliationRunRequest) -> str:
    payload = CanonicalSerializer.serialize(
        {
            "object_class": request.object_class,
            "organization_id": request.organization_id,
            "provider_code": request.provider_code,
            "scope": dict(request.scope),
            "watermark": dict(request.watermark),
        }
    )
    return hashlib.sha256(f"{RECONCILIATION_IDENTITY_VERSION}:{payload}".encode("utf-8")).hexdigest()


def compute_evidence_hash(payload: Mapping[str, Any]) -> str:
    canonical = CanonicalSerializer.serialize(dict(payload))
    return hashlib.sha256(f"{RECONCILIATION_EVIDENCE_VERSION}:{canonical}".encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_aware_utc(value: datetime | None, field_name: str) -> None:
    if value is None:
        raise ReconciliationEvidenceAmbiguous(f"{field_name} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReconciliationEvidenceAmbiguous(f"{field_name} must be timezone-aware")
