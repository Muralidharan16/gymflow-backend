from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pathlib import Path
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.provider_operations import (
    ProviderOperationResult,
)
from app.platform_billing.domain.reconciliation import (
    PROVIDER_AMBIGUOUS,
    PROVIDER_NOT_FOUND,
    PROVIDER_PENDING,
    PROVIDER_TERMINAL_FAILED,
    PROVIDER_TERMINAL_SUCCEEDED,
    SUPPORTED_PROVIDER_EVIDENCE_STATUSES,
    ProviderOperationEvidence,
    ReconciliationClaimLost,
    ReconciliationEvidenceAmbiguous,
    ReconciliationEvidenceStale,
    ReconciliationItemClaim,
    ReconciliationItemResult,
    ReconciliationItemSnapshot,
    ReconciliationProviderFailure,
    ReconciliationRunClaim,
    ReconciliationRunClaimLost,
    ReconciliationRunRequest,
    ReconciliationRunResult,
    ReconciliationRunSnapshot,
    ReconciliationTransitionRejected,
    require_aware_utc,
)
from app.platform_billing.providers.fake_checkout_evidence import (
    FakeCheckoutEvidenceStorageFailure,
    LocalEncryptedFakeCheckoutEvidenceStore,
    LocalFakeCheckoutProviderEvidenceReader,
)
from app.platform_billing.providers.fake_checkout_simulation import CONFIRM_CHECKOUT_OPERATION_TYPE
from app.platform_billing.providers.reconciliation import ProviderEvidenceReader
from app.platform_billing.repositories.provider_operations import (
    PlatformProviderOperationRepository,
)
from app.platform_billing.repositories.reconciliation import (
    PlatformReconciliationItemRepository,
    PlatformReconciliationRunRepository,
)


SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]
Clock = Callable[[], datetime]
DEFAULT_RECONCILIATION_LEASE = timedelta(minutes=5)
DEFAULT_FAKE_CHECKOUT_RECONCILIATION_DELAY = timedelta(seconds=30)
FAKE_CHECKOUT_RECONCILIATION_LIMIT = 100


class FakeCheckoutReconciliationDisabled(Exception):
    pass


class PlatformReconciliationService:
    def __init__(
        self,
        *,
        evidence_reader: ProviderEvidenceReader,
        session_factory: SessionFactory = AsyncSessionLocal,
        clock: Clock | None = None,
        lease: timedelta = DEFAULT_RECONCILIATION_LEASE,
    ):
        if lease.total_seconds() <= 0:
            raise ValueError("Reconciliation lease must be positive")
        self._evidence_reader = evidence_reader
        self._session_factory = session_factory
        self._clock = clock or _utc_now
        self._lease = lease

    async def reconcile(self, request: ReconciliationRunRequest) -> ReconciliationRunResult:
        run = await self.reserve_run(request)
        claim = await self.claim_run(run.id)
        if not claim.claimed:
            return ReconciliationRunResult(run=claim.run, discovered=0, processed=0, resolved=0, failed=0)

        try:
            page = await self._evidence_reader.list_operation_evidence(request)
        except ReconciliationProviderFailure as exc:
            run = await self._mark_run_failure(claim, str(exc) or "provider_failure")
            return ReconciliationRunResult(run=run, discovered=0, processed=0, resolved=0, failed=1)
        discovered = await self._discover_items(request, claim, page.evidence, dict(page.next_watermark))
        processed = 0
        resolved = 0
        failed = 0
        for item in discovered:
            item_claim = await self.claim_item(item.id, organization_id=item.organization_id)
            if not item_claim.claimed:
                continue
            result = await self.process_item_claim(item_claim)
            processed += 1
            if result.item.resolution_status in {"resolved", "ignored"}:
                resolved += 1
            elif result.item.resolution_status == "failed":
                failed += 1

        run = await self._finalize_run(claim, dict(page.next_watermark))
        return ReconciliationRunResult(
            run=run,
            discovered=len(discovered),
            processed=processed,
            resolved=resolved,
            failed=failed,
        )

    async def reserve_run(self, request: ReconciliationRunRequest) -> ReconciliationRunSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationRunRepository(session)
                return await repository.reserve(request)

    async def claim_run(self, run_id: uuid.UUID) -> ReconciliationRunClaim:
        now = self._clock()
        expires_at = now + self._lease
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationRunRepository(session)
                run = await repository.claim(run_id, now=now, expires_at=expires_at)
                return ReconciliationRunClaim(
                    run=run,
                    attempt_number=run.attempt_count,
                    claimed_at=run.claimed_at or now,
                    claim_expires_at=run.claim_expires_at or expires_at,
                    claimed=run.claimed,
                )

    async def claim_item(
        self,
        item_id: uuid.UUID,
        *,
        organization_id: uuid.UUID | None,
    ) -> ReconciliationItemClaim:
        now = self._clock()
        expires_at = now + self._lease
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationItemRepository(session)
                if organization_id is not None:
                    await repository.set_tenant_context(organization_id)
                item = await repository.claim(item_id, now=now, expires_at=expires_at)
                return ReconciliationItemClaim(
                    item=item,
                    attempt_number=item.attempt_count,
                    claimed_at=item.claimed_at or now,
                    claim_expires_at=item.claim_expires_at or expires_at,
                    claimed=item.claimed,
                )

    async def process_item_claim(self, claim: ReconciliationItemClaim) -> ReconciliationItemResult:
        try:
            evidence = await self._evidence_reader.fetch_operation_evidence(claim.item.evidence_ref)
            return await self._apply_item_evidence(claim, evidence)
        except ReconciliationClaimLost:
            raise
        except ReconciliationProviderFailure:
            return await self._mark_item_retryable(claim, "provider_retryable_failure")
        except Exception:
            return await self._mark_item_retryable(claim, "reconciliation_processing_failure")

    async def _discover_items(
        self,
        request: ReconciliationRunRequest,
        claim: ReconciliationRunClaim,
        evidence_items: tuple[ProviderOperationEvidence, ...],
        watermark_json: dict,
    ) -> list[ReconciliationItemSnapshot]:
        discovered: list[ReconciliationItemSnapshot] = []
        async with self._session_factory() as session:
            async with session.begin():
                run_repository = PlatformReconciliationRunRepository(session)
                await run_repository.assert_claim_owned(claim.run_id, expected_attempt_count=claim.attempt_number)
                operation_repository = PlatformProviderOperationRepository(session)
                item_repository = PlatformReconciliationItemRepository(session)
                for evidence in evidence_items:
                    organization_id = request.organization_id
                    operation = None
                    if organization_id is not None:
                        await item_repository.set_tenant_context(organization_id)
                        await operation_repository.set_tenant_context(organization_id)
                    if evidence.provider_code == request.provider_code:
                        operation = await operation_repository.get_by_external_operation_ref(
                            provider_code=evidence.provider_code,
                            external_operation_ref=evidence.external_operation_ref,
                        )
                        if operation is not None:
                            organization_id = operation.organization_id
                            await item_repository.set_tenant_context(organization_id)
                    if organization_id is None:
                        continue
                    classification = classify_discrepancy(operation, evidence)
                    item = await item_repository.discover(
                        run_id=claim.run_id,
                        organization_id=organization_id,
                        evidence=evidence,
                        discrepancy_classification=classification,
                        local_object_id=operation.id if operation is not None else None,
                    )
                    discovered.append(item)
        return discovered

    async def _apply_item_evidence(
        self,
        claim: ReconciliationItemClaim,
        evidence: ProviderOperationEvidence,
    ) -> ReconciliationItemResult:
        if claim.item.organization_id is None:
            return await self._mark_item_terminal(claim, "ignored", "unknown_mapping")
        async with self._session_factory() as session:
            async with session.begin():
                item_repository = PlatformReconciliationItemRepository(session)
                await item_repository.set_tenant_context(claim.item.organization_id)
                await item_repository.assert_claim_owned(
                    claim.item_id,
                    expected_attempt_count=claim.attempt_number,
                )
                operation_repository = PlatformProviderOperationRepository(session)
                await operation_repository.set_tenant_context(claim.item.organization_id)
                operation = await operation_repository.get_by_external_operation_ref(
                    provider_code=evidence.provider_code,
                    external_operation_ref=evidence.external_operation_ref,
                    for_update=True,
                )
                if operation is None or operation.id != claim.item.local_object_id:
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="unknown_mapping",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "unknown_mapping")
                if operation.provider_code != evidence.provider_code or operation.organization_id != claim.item.organization_id:
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="unknown_mapping",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "unknown_mapping", operation.id)
                if not _provider_evidence_matches_local_operation(operation, evidence):
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="ambiguous_provider_evidence",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "ambiguous_provider_evidence", operation.id)

                classification = classify_discrepancy(operation, evidence)
                if _is_fake_checkout_retryable_noop(evidence, classification):
                    item = await item_repository.retryable_failure(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        last_error_code=classification,
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "open", classification, operation.id)
                try:
                    classification, transition = resolve_operation_outcome(operation, evidence)
                except ReconciliationEvidenceStale:
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="stale_provider_evidence",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "stale_provider_evidence", operation.id)
                except ReconciliationEvidenceAmbiguous:
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="ambiguous_provider_evidence",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "ambiguous_provider_evidence", operation.id)
                except ReconciliationTransitionRejected:
                    item = await item_repository.resolve(
                        claim.item_id,
                        expected_attempt_count=claim.attempt_number,
                        resolution_status="ignored",
                        last_error_code="unsupported_local_mapping",
                        now=self._clock(),
                    )
                    return ReconciliationItemResult(item, "ignored", "unsupported_local_mapping", operation.id)
                if transition is not None:
                    await operation_repository.record_result(transition)
                item_status = "failed" if classification == "evidence_conflict" else "resolved"
                item = await item_repository.resolve(
                    claim.item_id,
                    expected_attempt_count=claim.attempt_number,
                    resolution_status=item_status,
                    last_error_code=None if item_status == "resolved" else classification,
                    now=self._clock(),
                )
                return ReconciliationItemResult(item, item_status, classification, operation.id)

    async def _mark_item_terminal(
        self,
        claim: ReconciliationItemClaim,
        resolution_status: str,
        classification: str,
    ) -> ReconciliationItemResult:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationItemRepository(session)
                if claim.item.organization_id is not None:
                    await repository.set_tenant_context(claim.item.organization_id)
                item = await repository.resolve(
                    claim.item_id,
                    expected_attempt_count=claim.attempt_number,
                    resolution_status=resolution_status,
                    last_error_code=classification if resolution_status != "resolved" else None,
                    now=self._clock(),
                )
                return ReconciliationItemResult(item, resolution_status, classification)

    async def _mark_item_retryable(
        self,
        claim: ReconciliationItemClaim,
        classification: str,
    ) -> ReconciliationItemResult:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationItemRepository(session)
                if claim.item.organization_id is not None:
                    await repository.set_tenant_context(claim.item.organization_id)
                item = await repository.retryable_failure(
                    claim.item_id,
                    expected_attempt_count=claim.attempt_number,
                    last_error_code=classification,
                    now=self._clock(),
                )
                return ReconciliationItemResult(item, "open", classification)

    async def _mark_run_failure(
        self,
        claim: ReconciliationRunClaim,
        classification: str,
    ) -> ReconciliationRunSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                repository = PlatformReconciliationRunRepository(session)
                return await repository.record_failure(
                    claim.run_id,
                    expected_attempt_count=claim.attempt_number,
                    last_error_code=classification,
                    now=self._clock(),
                )

    async def _finalize_run(
        self,
        claim: ReconciliationRunClaim,
        watermark_json: dict,
    ) -> ReconciliationRunSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                organization_id = claim.run.scope_json.get("organization_id")
                if organization_id:
                    await session.execute(
                        text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
                        {"org_id": str(organization_id)},
                    )
                repository = PlatformReconciliationRunRepository(session)
                return await repository.finalize_from_items(
                    claim.run_id,
                    expected_attempt_count=claim.attempt_number,
                    watermark_json=watermark_json,
                    now=self._clock(),
        )


async def reconcile_fake_checkout_operations(
    *,
    organization_id: uuid.UUID,
    session_factory: SessionFactory = AsyncSessionLocal,
    clock: Clock | None = None,
    eligibility_delay: timedelta = DEFAULT_FAKE_CHECKOUT_RECONCILIATION_DELAY,
    limit: int = FAKE_CHECKOUT_RECONCILIATION_LIMIT,
) -> ReconciliationRunResult:
    now = (clock or _utc_now)()
    _assert_fake_checkout_reconciliation_enabled()
    evidence_store = LocalEncryptedFakeCheckoutEvidenceStore(Path(settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR))
    evidence_reader = LocalFakeCheckoutProviderEvidenceReader(evidence_store)
    async with session_factory() as session:
        async with session.begin():
            operation_repository = PlatformProviderOperationRepository(session)
            await operation_repository.set_tenant_context(organization_id)
            candidates = await operation_repository.list_reconciliation_candidates(
                organization_id=organization_id,
                provider_code="fake",
                operation_type=CONFIRM_CHECKOUT_OPERATION_TYPE,
                statuses={"unknown", "in_progress"},
                older_than=now - eligibility_delay,
                limit=limit,
            )
            candidates = [
                candidate
                for candidate in candidates
                if candidate.external_operation_ref and candidate.external_operation_ref.startswith("fake_confirm_")
            ]

    request = ReconciliationRunRequest(
        provider_code="fake",
        organization_id=organization_id,
        scope={
            "source": "fake_checkout_reconciliation",
            "operation_type": CONFIRM_CHECKOUT_OPERATION_TYPE,
            "external_operation_refs": [candidate.external_operation_ref for candidate in candidates if candidate.external_operation_ref],
        },
        watermark={"selected_at": now.isoformat(), "candidate_count": len(candidates)},
    )
    service = PlatformReconciliationService(
        evidence_reader=evidence_reader,
        session_factory=session_factory,
        clock=clock,
    )
    return await service.reconcile(request)


def classify_discrepancy(operation, evidence: ProviderOperationEvidence) -> str:
    if evidence.provider_code not in {operation.provider_code if operation else evidence.provider_code}:
        return "unknown_mapping"
    if operation is None:
        return "unknown_mapping"
    if evidence.provider_status == PROVIDER_TERMINAL_SUCCEEDED:
        if operation.status == "unknown":
            return "local_unknown_provider_succeeded"
        if operation.status == "in_progress":
            return "local_in_progress_provider_succeeded"
        if operation.status == "succeeded":
            return "local_terminal_matches_provider"
        if operation.status == "failed":
            return "local_terminal_conflicts_provider"
    if evidence.provider_status == PROVIDER_TERMINAL_FAILED:
        if operation.status == "unknown":
            return "local_unknown_provider_failed"
        if operation.status == "in_progress":
            return "local_in_progress_provider_failed"
        if operation.status == "failed":
            return "local_terminal_matches_provider"
        if operation.status == "succeeded":
            return "local_terminal_conflicts_provider"
    if evidence.provider_status == PROVIDER_NOT_FOUND:
        if evidence.evidence_ref.startswith("fake-provider-evidence-missing:"):
            return "provider_evidence_not_found"
        return "provider_object_not_found"
    if evidence.provider_status == PROVIDER_PENDING:
        if evidence.evidence_ref.startswith("fake-provider-evidence:"):
            return "provider_evidence_pending"
    if evidence.provider_status == PROVIDER_AMBIGUOUS:
        return "ambiguous_provider_evidence"
    return "unsupported_local_mapping"


def resolve_operation_outcome(operation, evidence: ProviderOperationEvidence) -> tuple[str, ProviderOperationResult | None]:
    if evidence.provider_status not in SUPPORTED_PROVIDER_EVIDENCE_STATUSES:
        raise ReconciliationEvidenceAmbiguous("Unsupported provider evidence status")
    if evidence.provider_status in {PROVIDER_AMBIGUOUS, PROVIDER_PENDING}:
        raise ReconciliationEvidenceAmbiguous("Provider evidence is ambiguous")
    if evidence.provider_status == PROVIDER_NOT_FOUND:
        raise ReconciliationEvidenceAmbiguous("Provider object not found is not terminal evidence")
    require_aware_utc(evidence.observed_at, "observed_at")
    if operation.completed_at is not None and evidence.observed_at < operation.completed_at:
        raise ReconciliationEvidenceStale("Provider evidence is older than local evidence")

    if operation.status in {"succeeded", "failed"}:
        if operation.result_reference == evidence.evidence_ref and operation.result_evidence_sha256 != evidence.evidence_sha256:
            return "evidence_conflict", None
        if (operation.status == "succeeded" and evidence.provider_status == PROVIDER_TERMINAL_SUCCEEDED) or (
            operation.status == "failed" and evidence.provider_status == PROVIDER_TERMINAL_FAILED
        ):
            return "already_consistent", None
        return "evidence_conflict", None

    if operation.status not in {"unknown", "in_progress"}:
        raise ReconciliationTransitionRejected("Only unknown and in_progress operations may be reconciled")

    status = "succeeded" if evidence.provider_status == PROVIDER_TERMINAL_SUCCEEDED else "failed"
    error_classification = None if status == "succeeded" else "provider_reconciled_failure"
    return (
        f"resolved_{status}",
        ProviderOperationResult(
            operation_id=operation.id,
            status=status,
            external_operation_ref=evidence.external_operation_ref,
            error_classification=error_classification,
            result_evidence_sha256=evidence.evidence_sha256,
            result_reference=evidence.evidence_ref,
            provider_called=False,
        ),
    )


def _is_fake_checkout_retryable_noop(evidence: ProviderOperationEvidence, classification: str) -> bool:
    if classification not in {"provider_evidence_pending", "provider_evidence_not_found"}:
        return False
    return evidence.evidence_ref.startswith(("fake-provider-evidence:", "fake-provider-evidence-missing:"))


def _provider_evidence_matches_local_operation(operation, evidence: ProviderOperationEvidence) -> bool:
    if not evidence.evidence_ref.startswith("fake-provider-evidence:"):
        return True
    confirm_operation_id = evidence.safe_evidence.get("confirm_checkout_operation_id")
    if confirm_operation_id is not None and str(confirm_operation_id) != str(operation.id):
        return False
    return True


def _assert_fake_checkout_reconciliation_enabled() -> None:
    if not settings.PLATFORM_BILLING_FAKE_CHECKOUT_RECONCILIATION_ENABLED:
        raise FakeCheckoutReconciliationDisabled("fake_checkout_reconciliation_disabled")
    if settings.PLATFORM_BILLING_PROVIDER_MODE != "fake":
        raise FakeCheckoutReconciliationDisabled("fake_checkout_reconciliation_requires_fake_provider")
    if settings.ENVIRONMENT not in {"development", "test"}:
        raise FakeCheckoutReconciliationDisabled("fake_checkout_reconciliation_environment_denied")
    evidence_dir = settings.PLATFORM_BILLING_FAKE_PROVIDER_EVIDENCE_DIR
    if not evidence_dir or not evidence_dir.strip():
        raise FakeCheckoutReconciliationDisabled("fake_checkout_reconciliation_evidence_dir_missing")
    try:
        LocalEncryptedFakeCheckoutEvidenceStore(Path(evidence_dir))._validate_root()
    except FakeCheckoutEvidenceStorageFailure as exc:
        raise FakeCheckoutReconciliationDisabled("fake_checkout_reconciliation_evidence_dir_unusable") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
