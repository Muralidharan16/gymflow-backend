from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.platform_billing.domain.reconciliation import (
    PROVIDER_AMBIGUOUS,
    PROVIDER_NOT_FOUND,
    PROVIDER_PENDING,
    PROVIDER_TERMINAL_FAILED,
    PROVIDER_TERMINAL_SUCCEEDED,
    ProviderOperationEvidence,
    ReconciliationPage,
    ReconciliationProviderFailure,
    ReconciliationRunRequest,
    compute_evidence_hash,
)


class ProviderEvidenceReader(Protocol):
    async def list_operation_evidence(self, request: ReconciliationRunRequest) -> ReconciliationPage:
        """Return a bounded provider-neutral page of operation evidence."""

    async def fetch_operation_evidence(self, evidence_ref: str) -> ProviderOperationEvidence:
        """Return the provider-neutral evidence represented by a stored safe reference."""


Probe = Callable[[], bool | Awaitable[bool]]


@dataclass(frozen=True)
class FakeEvidenceCall:
    operation: str
    active_transaction_observed: bool
    evidence_ref: str | None = None


@dataclass(frozen=True)
class FakeEvidenceScript:
    provider_code: str
    external_operation_ref: str
    provider_status: str
    observed_at: datetime | None
    evidence_ref: str | None = None
    fail_on_list: bool = False
    fail_on_fetch: bool = False
    safe_evidence: Mapping[str, object] = field(default_factory=dict)


class DeterministicFakeEvidenceReader:
    def __init__(
        self,
        scripts: Sequence[FakeEvidenceScript] = (),
        *,
        transaction_probe: Probe | None = None,
        page_size: int | None = None,
    ):
        self._transaction_probe = transaction_probe
        self._page_size = page_size
        self.calls: list[FakeEvidenceCall] = []
        self._evidence_by_ref: dict[str, ProviderOperationEvidence] = {}
        self._scripts = tuple(scripts)
        for script in self._scripts:
            evidence = evidence_from_script(script)
            self._evidence_by_ref[evidence.evidence_ref] = evidence

    async def list_operation_evidence(self, request: ReconciliationRunRequest) -> ReconciliationPage:
        active_transaction = await self._probe()
        self.calls.append(FakeEvidenceCall("list_operation_evidence", active_transaction))
        if any(script.fail_on_list for script in self._scripts):
            raise ReconciliationProviderFailure("provider_retryable_failure")
        evidence = [
            self._evidence_by_ref[evidence_from_script(script).evidence_ref]
            for script in self._scripts
            if script.provider_code == request.provider_code
        ]
        evidence.sort(key=lambda item: item.evidence_ref)
        if self._page_size is not None:
            evidence = evidence[: self._page_size]
        watermark = {"last_evidence_ref": evidence[-1].evidence_ref} if evidence else dict(request.watermark)
        return ReconciliationPage(tuple(evidence), next_watermark=watermark)

    async def fetch_operation_evidence(self, evidence_ref: str) -> ProviderOperationEvidence:
        active_transaction = await self._probe()
        self.calls.append(FakeEvidenceCall("fetch_operation_evidence", active_transaction, evidence_ref))
        script = next(
            (script for script in self._scripts if evidence_from_script(script).evidence_ref == evidence_ref),
            None,
        )
        if script is not None and script.fail_on_fetch:
            raise ReconciliationProviderFailure("provider_retryable_failure")
        try:
            return self._evidence_by_ref[evidence_ref]
        except KeyError as exc:
            raise ReconciliationProviderFailure("provider_evidence_missing") from exc

    async def _probe(self) -> bool:
        if self._transaction_probe is None:
            return False
        result = self._transaction_probe()
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return bool(result)


def evidence_from_script(script: FakeEvidenceScript) -> ProviderOperationEvidence:
    safe_payload = {
        "external_operation_ref": script.external_operation_ref,
        "observed_at": script.observed_at,
        "provider_code": script.provider_code,
        "provider_status": script.provider_status,
        "safe_evidence": dict(script.safe_evidence),
    }
    evidence_sha256 = compute_evidence_hash(safe_payload)
    evidence_ref = script.evidence_ref or f"fake-reconciliation://{script.provider_code}/{script.external_operation_ref}/{evidence_sha256[:24]}"
    return ProviderOperationEvidence(
        provider_code=script.provider_code,
        external_operation_ref=script.external_operation_ref,
        provider_status=script.provider_status,
        observed_at=script.observed_at,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
        safe_evidence=safe_payload,
    )


def succeeded_script(provider_code: str, external_operation_ref: str, observed_at: datetime) -> FakeEvidenceScript:
    return FakeEvidenceScript(provider_code, external_operation_ref, PROVIDER_TERMINAL_SUCCEEDED, observed_at)


def failed_script(provider_code: str, external_operation_ref: str, observed_at: datetime) -> FakeEvidenceScript:
    return FakeEvidenceScript(provider_code, external_operation_ref, PROVIDER_TERMINAL_FAILED, observed_at)


def pending_script(provider_code: str, external_operation_ref: str, observed_at: datetime) -> FakeEvidenceScript:
    return FakeEvidenceScript(provider_code, external_operation_ref, PROVIDER_PENDING, observed_at)


def not_found_script(provider_code: str, external_operation_ref: str, observed_at: datetime) -> FakeEvidenceScript:
    return FakeEvidenceScript(provider_code, external_operation_ref, PROVIDER_NOT_FOUND, observed_at)


def ambiguous_script(provider_code: str, external_operation_ref: str, observed_at: datetime | None = None) -> FakeEvidenceScript:
    return FakeEvidenceScript(provider_code, external_operation_ref, PROVIDER_AMBIGUOUS, observed_at)
