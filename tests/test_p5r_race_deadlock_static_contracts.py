from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P5R_RACE_AND_DEADLOCK_INTERLEAVINGS.md"
RUNTIME = ROOT / "tests/test_p5r_race_deadlock_runtime.py"
WORKFLOW = ROOT / ".github/workflows/p5r-race-deadlock-pg16.yml"
LIFECYCLE = ROOT / "app/services/branch_lifecycle_service.py"
ROUTER = ROOT / "app/routers/branch_lifecycle.py"
DATABASE = ROOT / "app/core/database.py"
POLLER = ROOT / "app/tasks/branch_outbox_poller.py"
P4D = ROOT / "alembic/versions/zd07d8e9f0a3e_p4d_refund_obligation_resolution.py"


def test_certified_p5d_predecessor_is_recorded_exactly() -> None:
    source = DOC.read_text(encoding="utf-8")
    for phrase in (
        "c305b2951404e756a2bcdf9ac6c7a859bbdeba11",
        "0f30a99eb3e6ad7fe11461b527bcac37d7dc3ff5",
        "34813683351",
        "103879856422",
        "zj07d8e9f0a44",
        "69 passed",
        "6 passed",
        "91 passed",
    ):
        assert phrase in source


def test_scope_names_every_p5r_interleaving_and_hard_failure() -> None:
    source = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "same-branch api transition race",
        "last-operational-branch invariant race",
        "durable claim, reclaim and stale-fence race",
        "api versus lifecycle transaction-b race",
        "lifecycle-to-finance handoff race",
        "deadlock detection and production lock-order proof",
        "40p01",
        "55p03",
        "lost update",
        "stuck transition",
        "false terminal success",
        "broad table grant",
    ):
        assert phrase in source


def test_production_lifecycle_lock_order_is_org_then_branch_then_row() -> None:
    source = LIFECYCLE.read_text(encoding="utf-8")
    start = source.index("async def initiate_transition")
    end = source.index("\n    async def _record_checkpoint", start)
    method = source[start:end]

    org_lock = method.index('f"branch-lifecycle:org:{org_id}"')
    branch_lock = method.index('f"branch-lifecycle:branch:{branch_id}"')
    row_lock = method.index(".with_for_update()")
    revalidate = method.index("if branch_state.status != from_status")
    invariant = method.index("operational_count = await self.db.scalar")

    assert org_lock < branch_lock < row_lock < revalidate < invariant
    assert "Branch status changed concurrently; retry the transition" in method
    assert "A lifecycle transition is already in progress for this branch" in method


def test_api_lock_budget_and_55p03_mapping_are_bounded_without_hiding_deadlocks() -> None:
    database = DATABASE.read_text(encoding="utf-8")
    router = ROUTER.read_text(encoding="utf-8")
    start = router.index("async def _initiate_transition_with_contention_mapping")
    end = router.index("\n\n@router.get", start)
    helper = router[start:end]

    assert "_API_LOCK_TIMEOUT_MS = 500" in database
    assert 'if _db_sqlstate(exc) == "55P03"' in helper
    assert "status.HTTP_409_CONFLICT" in helper
    assert "Lifecycle transition is busy; retry the transition" in helper
    assert "40P01" not in helper
    assert "await _initiate_transition_with_contention_mapping(" in router


def test_worker_claim_and_delivery_are_skip_locked_and_monotonic_fenced() -> None:
    source = POLLER.read_text(encoding="utf-8")
    for phrase in (
        "FOR UPDATE SKIP LOCKED",
        "lease_fence = outbox_data.lease_fence + 1",
        "AND leased_by = :worker_id",
        "AND lease_fence = :lease_fence",
        "AND leased_until > pg_catalog.clock_timestamp()",
        "Lost lifecycle outbox lease before delivery",
    ):
        assert phrase in source


def test_runtime_uses_real_independent_transactions_not_mock_concurrency() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'P5R_RACE_FAULTS") != "1"',
        'P5R_DISPOSABLE_DATABASE") != _DATABASE',
        "NullPool",
        "asyncio.gather",
        "threading.Barrier",
        "DeadlockDetected",
        "40P01",
        "pg_stat_activity",
        "p5r_transaction_b_barrier",
        "p5r_refund_visibility_barrier",
        "_initiate_transition_with_contention_mapping",
    ):
        assert phrase in source
    assert "monkeypatch" not in source
    assert "Mock(" not in source
    assert "AsyncMock" not in source


def test_runtime_names_all_certified_races() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for name in (
        "test_same_branch_transition_race_commits_exactly_one_transaction_a",
        "test_last_operational_branch_race_preserves_org_invariant",
        "test_duplicate_claim_and_expired_reclaim_reject_stale_fence",
        "test_api_lock_timeout_is_bounded_then_retry_succeeds_after_transaction_b",
        "test_lifecycle_to_finance_handoff_is_invisible_until_commit",
        "test_deadlock_detector_canary_observes_exactly_one_40p01_victim",
    ):
        assert name in source


def test_finance_handoff_remains_evaluation_not_provider_authority() -> None:
    doc = DOC.read_text(encoding="utf-8")
    p4d = P4D.read_text(encoding="utf-8")
    assert "branch.refund_required" in doc
    assert "not provider refund authority" in doc
    assert "resolve_branch_refund_required" in p4d
    assert "provider refund API call" in p4d


def test_workflow_is_pg16_reduced_identity_exact_head_and_same_sha() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for phrase in (
        "ubuntu-24.04",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "auth_p5r_runtime",
        "app_test_runtime",
        "worker_test_runtime",
        "NOBYPASSRLS",
        "zj07d8e9f0a44",
        "tests/test_p5r_race_deadlock_runtime.py",
        "tests/test_p5r_race_deadlock_static_contracts.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P5R_SAME_BRANCH_RACE=PASS",
        "P5R_LAST_OPERATIONAL_RACE=PASS",
        "P5R_STALE_FENCE_RACE=PASS",
        "P5R_API_WORKER_RACE=PASS",
        "P5R_FINANCE_HANDOFF_RACE=PASS",
        "P5R_DEADLOCK_GUARD=PASS",
    ):
        assert phrase in source
    assert "continue-on-error" not in source
    assert "|| true" not in source
    assert "p5r_blocker_probe.py" not in source


def test_slice_does_not_broaden_privilege_or_later_phase_scope() -> None:
    executable = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (RUNTIME, WORKFLOW, ROUTER)
    )
    for forbidden in (
        "grant all on",
        "alter role app_test_runtime superuser",
        "alter role worker_test_runtime superuser",
        "p5-c certified",
        "p5-f certified",
        "refund provider execution",
    ):
        assert forbidden not in executable
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "NOBYPASSRLS" in workflow
    assert " BYPASSRLS" not in workflow.replace("NOBYPASSRLS", "")
