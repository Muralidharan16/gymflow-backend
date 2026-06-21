from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.platform_billing.domain.hashing import CanonicalSerializer


PROVIDER_OPERATION_HASH_VERSION = "provider-operation-v1"


class ProviderOperationStatus(str, Enum):
    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProviderOutcomeKind(str, Enum):
    SUCCESS = "success"
    BUSINESS_FAILURE = "business_failure"
    RETRYABLE_FAILURE = "retryable_failure"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = {
    ProviderOperationStatus.SUCCEEDED.value,
    ProviderOperationStatus.FAILED.value,
    ProviderOperationStatus.UNKNOWN.value,
}

LEGAL_TRANSITIONS = {
    ProviderOperationStatus.RESERVED.value: {
        ProviderOperationStatus.IN_PROGRESS.value,
        ProviderOperationStatus.SUCCEEDED.value,
        ProviderOperationStatus.FAILED.value,
        ProviderOperationStatus.UNKNOWN.value,
    },
    ProviderOperationStatus.IN_PROGRESS.value: {
        ProviderOperationStatus.SUCCEEDED.value,
        ProviderOperationStatus.FAILED.value,
        ProviderOperationStatus.UNKNOWN.value,
    },
    ProviderOperationStatus.UNKNOWN.value: {
        ProviderOperationStatus.SUCCEEDED.value,
        ProviderOperationStatus.FAILED.value,
    },
    ProviderOperationStatus.SUCCEEDED.value: set(),
    ProviderOperationStatus.FAILED.value: set(),
}


@dataclass(frozen=True)
class ProviderOperationRequest:
    organization_id: uuid.UUID
    provider_code: str
    operation_type: str
    idempotency_key: str
    amount_minor: int | None = None
    currency_code: str | None = None
    plan_version_id: uuid.UUID | None = None
    price_id: uuid.UUID | None = None
    provider_customer_ref: str | None = None
    provider_payment_method_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderOperationSnapshot:
    id: uuid.UUID
    organization_id: uuid.UUID
    provider_code: str
    operation_type: str
    idempotency_key: str
    canonical_request_sha256: str
    status: str
    external_operation_ref: str | None
    attempt_count: int
    result_evidence_sha256: str | None
    result_reference: str | None
    error_classification: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    was_created: bool = False
    execution_claimed: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ProviderOperationResult:
    operation_id: uuid.UUID
    status: str
    external_operation_ref: str | None
    error_classification: str | None
    result_evidence_sha256: str | None
    result_reference: str | None
    provider_called: bool


@dataclass(frozen=True)
class ProviderCallRequest:
    operation_id: uuid.UUID
    organization_id: uuid.UUID
    provider_code: str
    operation_type: str
    amount_minor: int | None
    currency_code: str | None
    plan_version_id: uuid.UUID | None
    price_id: uuid.UUID | None
    provider_customer_ref: str | None
    provider_payment_method_ref: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ProviderCallResult:
    outcome: ProviderOutcomeKind
    external_operation_ref: str | None = None
    result_reference: str | None = None
    result_evidence_sha256: str | None = None
    error_classification: str | None = None


class ProviderOperationError(Exception):
    """Base class for Phase 4 provider-operation domain errors."""


class ProviderOperationNotFound(ProviderOperationError):
    pass


class ProviderOperationConflict(ProviderOperationError):
    pass


class IdempotencyConflict(ProviderOperationConflict):
    pass


class IllegalProviderOperationTransition(ProviderOperationConflict):
    pass


class ProviderBusinessFailure(ProviderOperationError):
    pass


class ProviderTechnicalFailure(ProviderOperationError):
    pass


class ProviderOutcomeUnknown(ProviderOperationError):
    pass


class ProviderResultPersistenceFailure(ProviderOperationError):
    pass


def canonical_provider_request_payload(request: ProviderOperationRequest) -> dict[str, Any]:
    return {
        "amount_minor": request.amount_minor,
        "currency_code": request.currency_code,
        "metadata": request.metadata,
        "operation_type": request.operation_type,
        "organization_id": request.organization_id,
        "plan_version_id": request.plan_version_id,
        "price_id": request.price_id,
        "provider_code": request.provider_code,
        "provider_customer_ref": request.provider_customer_ref,
        "provider_payment_method_ref": request.provider_payment_method_ref,
    }


def canonical_provider_request_json(request: ProviderOperationRequest) -> str:
    return CanonicalSerializer.serialize(canonical_provider_request_payload(request))


def compute_provider_request_hash(request: ProviderOperationRequest) -> str:
    payload = f"{PROVIDER_OPERATION_HASH_VERSION}:{canonical_provider_request_json(request)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provider_call_request_from_operation(
    *,
    operation_id: uuid.UUID,
    request: ProviderOperationRequest,
) -> ProviderCallRequest:
    return ProviderCallRequest(
        operation_id=operation_id,
        organization_id=request.organization_id,
        provider_code=request.provider_code,
        operation_type=request.operation_type,
        amount_minor=request.amount_minor,
        currency_code=request.currency_code,
        plan_version_id=request.plan_version_id,
        price_id=request.price_id,
        provider_customer_ref=request.provider_customer_ref,
        provider_payment_method_ref=request.provider_payment_method_ref,
        metadata=dict(request.metadata),
    )


def result_for_outcome(
    operation_id: uuid.UUID,
    call_result: ProviderCallResult,
) -> ProviderOperationResult:
    if call_result.outcome is ProviderOutcomeKind.SUCCESS:
        status = ProviderOperationStatus.SUCCEEDED.value
        error_classification = None
    elif call_result.outcome is ProviderOutcomeKind.BUSINESS_FAILURE:
        status = ProviderOperationStatus.FAILED.value
        error_classification = call_result.error_classification or "provider_business_failure"
    elif call_result.outcome is ProviderOutcomeKind.RETRYABLE_FAILURE:
        status = ProviderOperationStatus.FAILED.value
        error_classification = call_result.error_classification or "provider_retryable_failure"
    elif call_result.outcome in {ProviderOutcomeKind.TIMEOUT, ProviderOutcomeKind.UNKNOWN}:
        status = ProviderOperationStatus.UNKNOWN.value
        error_classification = call_result.error_classification or call_result.outcome.value
    else:
        raise ProviderOutcomeUnknown("Unsupported provider outcome")

    evidence = call_result.result_evidence_sha256
    if evidence is None:
        evidence_payload = {
            "operation_id": operation_id,
            "outcome": call_result.outcome.value,
            "external_operation_ref": call_result.external_operation_ref,
            "result_reference": call_result.result_reference,
            "error_classification": error_classification,
        }
        serialized = CanonicalSerializer.serialize(evidence_payload)
        evidence = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    return ProviderOperationResult(
        operation_id=operation_id,
        status=status,
        external_operation_ref=call_result.external_operation_ref,
        error_classification=error_classification,
        result_evidence_sha256=evidence,
        result_reference=call_result.result_reference,
        provider_called=True,
    )


def ensure_legal_transition(current_status: str, next_status: str) -> None:
    if current_status == next_status and current_status in TERMINAL_STATUSES:
        return
    if next_status not in LEGAL_TRANSITIONS.get(current_status, set()):
        raise IllegalProviderOperationTransition(
            f"Illegal provider operation transition {current_status!r} -> {next_status!r}"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
