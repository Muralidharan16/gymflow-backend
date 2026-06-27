from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.platform_billing.domain.reconciliation import (
    ReconciliationClaimLost,
    ReconciliationRunClaimLost,
    ReconciliationRunRequest,
    compute_run_identity,
)
from app.platform_billing.domain.provider_operations import ProviderOperationResult
from app.platform_billing.providers.reconciliation import (
    DeterministicFakeEvidenceReader,
    FakeEvidenceScript,
    ambiguous_script,
    failed_script,
    not_found_script,
    pending_script,
    succeeded_script,
)
from app.platform_billing.repositories.reconciliation import (
    PlatformReconciliationItemRepository,
    PlatformReconciliationRunRepository,
)
from app.platform_billing.repositories.provider_operations import (
    PlatformProviderOperationRepository,
)
from app.platform_billing.services.reconciliation import (
    DEFAULT_RECONCILIATION_LEASE,
    PlatformReconciliationService,
)
from tests.platform_billing.test_phase1_schema import (
    ORG_1,
    ORG_2,
    cleanup_phase1_tables,
    seed_organizations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
OLDER = datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc)
NEWER = datetime(2026, 6, 22, 6, 0, tzinfo=timezone.utc)
SHA_R = "d" * 64


class MutableClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def request(
    *,
    provider_code: str = "fake",
    organization_id: str = ORG_1,
    watermark: dict[str, object] | None = None,
    scope: dict[str, object] | None = None,
) -> ReconciliationRunRequest:
    return ReconciliationRunRequest(
        provider_code=provider_code,
        organization_id=uuid.UUID(organization_id),
        scope=scope or {"window": "phase4d"},
        watermark=watermark or {"cursor": "0"},
    )


def service(
    scripts: list[FakeEvidenceScript],
    *,
    clock: MutableClock | None = None,
    transaction_probe=None,
) -> PlatformReconciliationService:
    return PlatformReconciliationService(
        evidence_reader=DeterministicFakeEvidenceReader(scripts, transaction_probe=transaction_probe),
        clock=clock or MutableClock(),
    )


async def seed_operation(
    *,
    status: str,
    external_ref: str,
    organization_id: str = ORG_1,
    provider_code: str = "fake",
    completed_at: datetime | None = OLDER,
    evidence_hash: str | None = SHA_R,
    error_classification: str | None = None,
) -> uuid.UUID:
    operation_id = uuid.uuid4()
    terminal = status in {"succeeded", "failed", "unknown"}
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO platform_provider_operations (
                    id, organization_id, provider_code, operation_type,
                    idempotency_key, canonical_request_sha256, status,
                    external_operation_ref, attempt_count, result_evidence_sha256,
                    result_reference, error_classification, completed_at
                )
                VALUES (
                    :id, :org_id, :provider_code, 'checkout_session_reserve',
                    :idempotency_key, :request_hash, :status, :external_ref,
                    1, :result_hash, :result_reference, :error_classification,
                    :completed_at
                )
                """
            ),
            {
                "id": operation_id,
                "org_id": organization_id,
                "provider_code": provider_code,
                "idempotency_key": f"phase4d-{operation_id}",
                "request_hash": SHA_R,
                "status": status,
                "external_ref": external_ref,
                "result_hash": evidence_hash if terminal else None,
                "result_reference": f"seed:{external_ref}" if terminal else None,
                "error_classification": error_classification or ("seed_failure" if status == "failed" else None),
                "completed_at": completed_at if terminal else None,
            },
        )
        await session.commit()
    return operation_id


async def fetch_operation(operation_id: uuid.UUID, organization_id: str = ORG_1):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        result = await session.execute(
            text(
                """
                SELECT status, result_evidence_sha256, result_reference,
                       error_classification, completed_at
                FROM platform_provider_operations
                WHERE id = :id
                """
            ),
            {"id": operation_id},
        )
        return result.mappings().one()


async def fetch_run(run_id: uuid.UUID):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT status, claim_state, attempt_count, scanned_count,
                       discrepancy_count, resolved_count, failed_count, last_error_code
                FROM platform_reconciliation_runs
                WHERE id = :id
                """
            ),
            {"id": run_id},
        )
        return result.mappings().one()


async def fetch_item(external_ref: str, organization_id: str = ORG_1):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": organization_id},
        )
        result = await session.execute(
            text(
                """
                SELECT id, resolution_status, claim_state, attempt_count,
                       discrepancy_classification, evidence_sha256, evidence_ref,
                       last_error_code
                FROM platform_reconciliation_items
                WHERE external_object_ref = :external_ref
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"external_ref": external_ref},
        )
        return result.mappings().one_or_none()


async def item_count() -> int:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
            {"org_id": ORG_1},
        )
        return await session.scalar(text("SELECT count(*) FROM platform_reconciliation_items"))


@pytest.mark.asyncio
async def test_deterministic_run_identity_duplicate_replay_and_different_watermark():
    await cleanup_phase1_tables()
    await seed_organizations()
    first_request = request()
    same_request = request(scope={"window": "phase4d"}, watermark={"cursor": "0"})
    different_request = request(watermark={"cursor": "1"})
    assert compute_run_identity(first_request) == compute_run_identity(same_request)
    assert compute_run_identity(first_request) != compute_run_identity(different_request)

    svc = service([])
    first = await svc.reserve_run(first_request)
    replay = await svc.reserve_run(same_request)
    different = await svc.reserve_run(different_request)

    assert first.id == replay.id
    assert first.was_created is True
    assert replay.was_created is False
    assert different.id != first.id


@pytest.mark.asyncio
async def test_concurrent_run_creation_creates_one_row_and_run_claim_lifecycle():
    await cleanup_phase1_tables()
    await seed_organizations()
    clock = MutableClock()
    svc = service([], clock=clock)
    run_a, run_b = await asyncio.gather(svc.reserve_run(request()), svc.reserve_run(request()))
    assert run_a.id == run_b.id

    claim_a = await svc.claim_run(run_a.id)
    claim_b = await svc.claim_run(run_a.id)
    assert claim_a.claimed is True
    assert claim_a.attempt_number == 1
    assert claim_b.claimed is False
    assert claim_b.attempt_number == 1

    clock.advance(DEFAULT_RECONCILIATION_LEASE)
    exact_cutoff = await svc.claim_run(run_a.id)
    assert exact_cutoff.claimed is False
    clock.advance(timedelta(microseconds=1))
    reclaimed = await svc.claim_run(run_a.id)
    assert reclaimed.claimed is True
    assert reclaimed.attempt_number == 2


@pytest.mark.asyncio
async def test_terminal_run_cannot_be_claimed_and_stale_run_worker_is_fenced():
    await cleanup_phase1_tables()
    await seed_organizations()
    clock = MutableClock()
    svc = service([succeeded_script("fake", "fake_op_run_done", NEWER)], clock=clock)
    await seed_operation(status="in_progress", external_ref="fake_op_run_done")
    result = await svc.reconcile(request())
    assert result.run.status == "succeeded"
    assert (await svc.claim_run(result.run.id)).claimed is False

    another = await svc.reserve_run(request(watermark={"cursor": "fenced"}))
    stale = await svc.claim_run(another.id)
    clock.advance(DEFAULT_RECONCILIATION_LEASE + timedelta(microseconds=1))
    await svc.claim_run(another.id)
    with pytest.raises(ReconciliationRunClaimLost):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                repo = PlatformReconciliationRunRepository(session)
                await repo.mark_running_idle(
                    stale.run_id,
                    expected_attempt_count=stale.attempt_number,
                    watermark_json={},
                    now=clock(),
                )


@pytest.mark.asyncio
async def test_evidence_fetch_happens_after_run_claim_without_transaction_or_row_lock():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_operation(status="in_progress", external_ref="fake_op_boundary")
    sessions = []
    lock_probe_results: list[bool] = []

    def tracking_session_factory():
        session = AsyncSessionLocal()
        sessions.append(session)
        return session

    async def transaction_probe() -> bool:
        assert all(not session.in_transaction() for session in sessions)
        async with AsyncSessionLocal() as probe_session:
            run_id = (await probe_session.scalar(text("SELECT id FROM platform_reconciliation_runs LIMIT 1")))
            if run_id is None:
                lock_probe_results.append(True)
                return False
            try:
                await probe_session.execute(
                    text("SELECT id FROM platform_reconciliation_runs WHERE id = :run_id FOR UPDATE NOWAIT"),
                    {"run_id": run_id},
                )
                lock_probe_results.append(True)
            except Exception:
                lock_probe_results.append(False)
            finally:
                await probe_session.rollback()
        return False

    svc = PlatformReconciliationService(
        evidence_reader=DeterministicFakeEvidenceReader(
            [succeeded_script("fake", "fake_op_boundary", NEWER)],
            transaction_probe=transaction_probe,
        ),
        session_factory=tracking_session_factory,
        clock=MutableClock(),
    )
    result = await svc.reconcile(request())
    assert result.run.status == "succeeded"
    assert lock_probe_results and all(lock_probe_results)
    assert all(not session.in_transaction() for session in sessions)


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_item_discovery_create_one_item():
    await cleanup_phase1_tables()
    await seed_organizations()
    await seed_operation(status="in_progress", external_ref="fake_op_dedupe")
    script = succeeded_script("fake", "fake_op_dedupe", NEWER)
    svc = service([script])
    run = await svc.reserve_run(request())
    claim = await svc.claim_run(run.id)
    page = await svc._evidence_reader.list_operation_evidence(request())
    first, second = await asyncio.gather(
        svc._discover_items(request(), claim, page.evidence, dict(page.next_watermark)),
        svc._discover_items(request(), claim, page.evidence, dict(page.next_watermark)),
    )
    assert first[0].id == second[0].id
    assert await item_count() == 1
    item = await fetch_item("fake_op_dedupe")
    assert item is not None
    assert len(item["evidence_sha256"]) == 64
    assert item["evidence_ref"].startswith("fake-reconciliation://")


@pytest.mark.asyncio
async def test_item_claim_stale_reclaim_fencing_and_terminal_reclaim():
    await cleanup_phase1_tables()
    await seed_organizations()
    clock = MutableClock()
    await seed_operation(status="in_progress", external_ref="fake_op_item_claim")
    svc = service([succeeded_script("fake", "fake_op_item_claim", NEWER)], clock=clock)
    run = await svc.reserve_run(request())
    run_claim = await svc.claim_run(run.id)
    page = await svc._evidence_reader.list_operation_evidence(request())
    item = (await svc._discover_items(request(), run_claim, page.evidence, dict(page.next_watermark)))[0]

    claim_a = await svc.claim_item(item.id, organization_id=item.organization_id)
    claim_b = await svc.claim_item(item.id, organization_id=item.organization_id)
    assert claim_a.claimed is True
    assert claim_b.claimed is False
    clock.advance(DEFAULT_RECONCILIATION_LEASE + timedelta(microseconds=1))
    claim_c = await svc.claim_item(item.id, organization_id=item.organization_id)
    assert claim_c.claimed is True
    assert claim_c.attempt_number == 2
    with pytest.raises(ReconciliationClaimLost):
        await svc.process_item_claim(claim_a)
    assert (await svc.process_item_claim(claim_c)).status == "resolved"
    terminal = await svc.claim_item(item.id, organization_id=item.organization_id)
    assert terminal.claimed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "script_factory", "expected_status"),
    [
        ("unknown", succeeded_script, "succeeded"),
        ("unknown", failed_script, "failed"),
        ("in_progress", succeeded_script, "succeeded"),
        ("in_progress", failed_script, "failed"),
    ],
)
async def test_successful_evidence_backed_operation_resolution(initial_status, script_factory, expected_status):
    await cleanup_phase1_tables()
    await seed_organizations()
    external_ref = f"fake_op_{initial_status}_{expected_status}"
    operation_id = await seed_operation(status=initial_status, external_ref=external_ref, completed_at=OLDER)
    result = await service([script_factory("fake", external_ref, NEWER)]).reconcile(request())
    assert result.run.status == "succeeded"
    operation = await fetch_operation(operation_id)
    assert operation["status"] == expected_status
    item = await fetch_item(external_ref)
    assert item["resolution_status"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "script_factory"),
    [
        ("succeeded", succeeded_script),
        ("failed", failed_script),
    ],
)
async def test_terminal_matching_evidence_is_noop_and_does_not_double_count(initial_status, script_factory):
    await cleanup_phase1_tables()
    await seed_organizations()
    external_ref = f"fake_op_terminal_match_{initial_status}"
    operation_id = await seed_operation(status=initial_status, external_ref=external_ref, completed_at=OLDER)
    svc = service([script_factory("fake", external_ref, NEWER)])
    first = await svc.reconcile(request())
    second = await svc.reconcile(request())

    assert first.run.resolved_count == 1
    assert second.discovered == 0
    operation = await fetch_operation(operation_id)
    assert operation["status"] == initial_status
    assert (await item_count()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "script_factory"),
    [
        ("succeeded", failed_script),
        ("failed", succeeded_script),
    ],
)
async def test_terminal_conflicting_evidence_preserves_local_operation(initial_status, script_factory):
    await cleanup_phase1_tables()
    await seed_organizations()
    external_ref = f"fake_op_terminal_conflict_{initial_status}"
    operation_id = await seed_operation(status=initial_status, external_ref=external_ref, completed_at=OLDER)
    before = await fetch_operation(operation_id)

    result = await service([script_factory("fake", external_ref, NEWER)]).reconcile(request())

    assert result.run.status == "failed"
    after = await fetch_operation(operation_id)
    assert after["status"] == before["status"]
    assert after["result_evidence_sha256"] == before["result_evidence_sha256"]
    item = await fetch_item(external_ref)
    assert item["resolution_status"] == "failed"
    assert item["last_error_code"] == "evidence_conflict"


@pytest.mark.asyncio
async def test_stale_missing_ambiguous_not_found_and_unknown_mapping_are_safe():
    await cleanup_phase1_tables()
    await seed_organizations()
    stale_id = await seed_operation(status="succeeded", external_ref="fake_op_stale", completed_at=NEWER)
    missing_ts_id = await seed_operation(status="in_progress", external_ref="fake_op_missing_ts")
    not_found_id = await seed_operation(status="unknown", external_ref="fake_op_not_found")
    ambiguous_id = await seed_operation(status="in_progress", external_ref="fake_op_ambiguous")
    scripts = [
        succeeded_script("fake", "fake_op_stale", OLDER),
        FakeEvidenceScript("fake", "fake_op_missing_ts", "succeeded", None),
        not_found_script("fake", "fake_op_not_found", NEWER),
        ambiguous_script("fake", "fake_op_ambiguous", NEWER),
        succeeded_script("fake", "fake_op_unknown_mapping", NEWER),
    ]

    result = await service(scripts).reconcile(request())

    assert result.run.status == "succeeded"
    assert (await fetch_operation(stale_id))["status"] == "succeeded"
    assert (await fetch_operation(missing_ts_id))["status"] == "in_progress"
    assert (await fetch_operation(not_found_id))["status"] == "unknown"
    assert (await fetch_operation(ambiguous_id))["status"] == "in_progress"
    assert (await fetch_item("fake_op_stale"))["last_error_code"] == "stale_provider_evidence"
    assert (await fetch_item("fake_op_missing_ts"))["last_error_code"] == "ambiguous_provider_evidence"
    assert (await fetch_item("fake_op_not_found"))["last_error_code"] == "ambiguous_provider_evidence"
    assert (await fetch_item("fake_op_ambiguous"))["last_error_code"] == "ambiguous_provider_evidence"
    assert (await fetch_item("fake_op_unknown_mapping"))["last_error_code"] == "unknown_mapping"


@pytest.mark.asyncio
async def test_cross_provider_and_cross_tenant_mapping_are_denied():
    await cleanup_phase1_tables()
    await seed_organizations()
    operation_id = await seed_operation(status="in_progress", external_ref="fake_op_cross_provider", provider_code="another_fake")
    cross_tenant_id = await seed_operation(
        status="in_progress",
        external_ref="fake_op_cross_tenant",
        organization_id=ORG_2,
        provider_code="fake",
    )

    await service(
        [
            succeeded_script("fake", "fake_op_cross_provider", NEWER),
            succeeded_script("fake", "fake_op_cross_tenant", NEWER),
        ]
    ).reconcile(request())

    assert (await fetch_operation(operation_id))["status"] == "in_progress"
    assert (await fetch_operation(cross_tenant_id, organization_id=ORG_2))["status"] == "in_progress"
    assert (await fetch_item("fake_op_cross_provider"))["last_error_code"] == "unknown_mapping"
    assert (await fetch_item("fake_op_cross_tenant"))["last_error_code"] == "unknown_mapping"


@pytest.mark.asyncio
async def test_webhook_wins_reconciliation_wins_and_conflicting_race_are_safe():
    await cleanup_phase1_tables()
    await seed_organizations()
    webhook_first = await seed_operation(status="unknown", external_ref="fake_op_webhook_wins", completed_at=OLDER)
    recon_first = await seed_operation(status="unknown", external_ref="fake_op_recon_wins", completed_at=OLDER)
    conflict = await seed_operation(status="unknown", external_ref="fake_op_conflict_race", completed_at=OLDER)
    provider_observed_at = datetime.now(timezone.utc) + timedelta(days=1)
    svc = service(
        [
            succeeded_script("fake", "fake_op_webhook_wins", provider_observed_at),
            succeeded_script("fake", "fake_op_recon_wins", provider_observed_at),
            succeeded_script("fake", "fake_op_conflict_race", provider_observed_at),
        ]
    )
    run = await svc.reserve_run(request())
    run_claim = await svc.claim_run(run.id)
    page = await svc._evidence_reader.list_operation_evidence(request())
    items = await svc._discover_items(request(), run_claim, page.evidence, dict(page.next_watermark))
    claims = {item.external_object_ref: await svc.claim_item(item.id, organization_id=item.organization_id) for item in items}

    async with AsyncSessionLocal() as session:
        async with session.begin():
            repo = PlatformProviderOperationRepository(session)
            await repo.set_tenant_context(uuid.UUID(ORG_1))
            await repo.record_result(
                ProviderOperationResult(webhook_first, "succeeded", "fake_op_webhook_wins", None, "e" * 64, "webhook:success", False)
            )
            await repo.record_result(
                ProviderOperationResult(conflict, "failed", "fake_op_conflict_race", "webhook_failed", "f" * 64, "webhook:failed", False)
            )

    assert (await svc.process_item_claim(claims["fake_op_webhook_wins"])).classification == "already_consistent"
    assert (await svc.process_item_claim(claims["fake_op_recon_wins"])).status == "resolved"
    assert (await svc.process_item_claim(claims["fake_op_conflict_race"])).classification == "evidence_conflict"
    assert (await fetch_operation(webhook_first))["status"] == "succeeded"
    assert (await fetch_operation(recon_first))["status"] == "succeeded"
    assert (await fetch_operation(conflict))["status"] == "failed"


@pytest.mark.asyncio
async def test_provider_failure_retry_and_partial_run_do_not_double_count():
    await cleanup_phase1_tables()
    await seed_organizations()
    ok_id = await seed_operation(status="in_progress", external_ref="fake_op_partial_ok")
    retry_id = await seed_operation(status="in_progress", external_ref="fake_op_partial_retry")
    scripts = [
        succeeded_script("fake", "fake_op_partial_ok", NEWER),
        FakeEvidenceScript("fake", "fake_op_partial_retry", "succeeded", NEWER, fail_on_fetch=True),
    ]
    result = await service(scripts).reconcile(request())
    assert result.run.status == "running"
    assert (await fetch_operation(ok_id))["status"] == "succeeded"
    assert (await fetch_operation(retry_id))["status"] == "in_progress"
    retry_item = await fetch_item("fake_op_partial_retry")
    assert retry_item["resolution_status"] == "open"
    assert retry_item["last_error_code"] == "provider_retryable_failure"

    recovered = await service([succeeded_script("fake", "fake_op_partial_retry", NEWER)]).reconcile(request())
    assert recovered.run.status == "succeeded"
    assert recovered.run.resolved_count == 2
    assert (await fetch_operation(retry_id))["status"] == "succeeded"


def test_phase4d_safety_guardrails_remain_invisible_and_non_enforcing():
    from app.core.config import settings

    assert settings.PLATFORM_BILLING_CHECKOUT is False
    assert settings.PLATFORM_BILLING_WEBHOOK_PROCESSING is False
    assert settings.PLATFORM_BILLING_DUNNING_TRANSITIONS is False
    assert settings.PLATFORM_BILLING_NOTIFICATIONS is False
    assert settings.PLATFORM_BILLING_ENFORCEMENT is False
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "completion.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "simulation.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "callback.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "api" / "browser_return.py").exists()
    assert not (REPO_ROOT / "app" / "platform_billing" / "tasks" / "reconciliation.py").exists()

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            REPO_ROOT / "app" / "platform_billing" / "domain" / "reconciliation.py",
            REPO_ROOT / "app" / "platform_billing" / "providers" / "reconciliation.py",
            REPO_ROOT / "app" / "platform_billing" / "repositories" / "reconciliation.py",
            REPO_ROOT / "app" / "platform_billing" / "services" / "reconciliation.py",
        ]
    ).lower()
    for token in ("requests.", "httpx.", "razorpay", "cashfree", "stripe", "api_key", "secret_key"):
        assert token not in source
    assert "platformsubscription" not in source
    assert "platformentitlementprojection" not in source
    assert "platformaccessprojection" not in source
