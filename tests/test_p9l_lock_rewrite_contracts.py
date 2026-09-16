from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/architecture/p9_data_protection_recovery_matrix.json"
SCOPE = ROOT / "docs/architecture/P9L_LOCK_REWRITE_ANALYSIS.md"
ACCEPTANCE = ROOT / "docs/architecture/P9_ACCEPTANCE_MATRIX.md"
WORKFLOW = ROOT / ".github/workflows/p9l-lock-rewrite-analysis.yml"
PROBE = ROOT / "scripts/p9l_lock_rewrite_probe.py"

P9_BRANCH = "hardening/p9-data-protection-disaster-recovery"
PREDECESSOR = "zj07d8e9f0a44"
HEAD = "zk07d8e9f0a45"

LOCK_TIMEOUT_MS = 1500
STATEMENT_TIMEOUT_MS = 15000
LOCK_WAIT_BUDGET_MS = 750
MIGRATION_DURATION_BUDGET_MS = 10000
CONTROLLED_BLOCK_HOLD_MS = 200
SAMPLE_INTERVAL_MS = 5

MARKERS = {
    "P9L_CONTROLLED_CONTENTION_OBSERVED=PASS",
    "P9L_ACQUIRED_LOCK_MODES_CAPTURED=PASS",
    "P9L_NO_UNEXPECTED_ACCESS_EXCLUSIVE=PASS",
    "P9L_LOCK_WAIT_BOUNDED=PASS",
    "P9_LOCK_BUDGET=PASS",
    "P9_TABLE_REWRITE_ANALYSIS=PASS",
    "P9_MIGRATION_DURATION_MEASURED=PASS",
    "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
}


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_p9l_files_exist_and_freeze_exact_runtime_budget() -> None:
    for path in (MATRIX, SCOPE, ACCEPTANCE, WORKFLOW, PROBE):
        assert path.is_file(), path

    contract = _matrix()["lock_and_rewrite_analysis"]
    assert contract["database_lock_timeout_ms"] == LOCK_TIMEOUT_MS
    assert contract["database_statement_timeout_ms"] == STATEMENT_TIMEOUT_MS
    assert contract["observed_lock_wait_budget_ms"] == LOCK_WAIT_BUDGET_MS
    assert contract["migration_wall_clock_budget_ms"] == MIGRATION_DURATION_BUDGET_MS
    assert contract["controlled_metadata_block_hold_ms"] == CONTROLLED_BLOCK_HOLD_MS
    assert contract["sample_interval_ms"] == SAMPLE_INTERVAL_MS
    assert contract["critical_relations"] == [
        "organizations",
        "org_branches",
        "branch_outbox_events",
    ]

    scope = SCOPE.read_text(encoding="utf-8")
    for value in (
        PREDECESSOR,
        HEAD,
        f"{LOCK_TIMEOUT_MS} ms",
        f"{STATEMENT_TIMEOUT_MS} ms",
        f"{LOCK_WAIT_BUDGET_MS} ms",
        f"{MIGRATION_DURATION_BUDGET_MS} ms",
        f"{CONTROLLED_BLOCK_HOLD_MS} ms",
        f"{SAMPLE_INTERVAL_MS} ms",
    ):
        assert value in scope
    for marker in MARKERS:
        assert marker in scope


def test_p9l_probe_observes_real_locks_blockers_and_monotonic_duration() -> None:
    source = PROBE.read_text(encoding="utf-8")
    for token in (
        'EXPECTED_PREDECESSOR = "zj07d8e9f0a44"',
        'EXPECTED_HEAD = "zk07d8e9f0a45"',
        "time.monotonic_ns()",
        "pg_catalog.pg_stat_activity",
        "pg_catalog.pg_locks",
        "pg_catalog.pg_blocking_pids(pid)",
        "p9l_observer",
        "p9l_reader",
        "p9l_writer",
        "p9l_version_blocker",
        "AccessShareLock",
        "RowExclusiveLock",
        "ShareLock",
        "AccessExclusiveLock",
        "branch_outbox_events",
        "alembic_version",
        "observed_lock_wait_ms",
        "max_contiguous_lock_wait_ms",
    ):
        assert token in source


def test_p9l_probe_detects_rewrite_and_relation_size_without_mutating_business_rows() -> None:
    source = PROBE.read_text(encoding="utf-8")
    for token in (
        "pg_catalog.pg_relation_filenode",
        "pg_catalog.pg_relation_size",
        "pg_catalog.pg_total_relation_size",
        '"rewrites_detected"',
        '"changed_heap_sizes"',
        '"relation_size_bytes"',
        '"total_size_bytes"',
        '"no_table_rewrite"',
    ):
        assert token in source
    assert "LOCK TABLE public.branch_outbox_events IN ACCESS SHARE MODE" in source
    assert "LOCK TABLE public.branch_outbox_events IN ROW EXCLUSIVE MODE" in source
    assert "LOCK TABLE public.alembic_version IN SHARE MODE" in source
    lowered = source.lower()
    assert "update public.branch_outbox_events" not in lowered
    assert "delete from public.branch_outbox_events" not in lowered
    assert "truncate public.branch_outbox_events" not in lowered
    assert "disable trigger" not in lowered
    assert "session_replication_role" not in lowered


def test_p9l_workflow_is_exact_head_real_pg16_and_bounded() -> None:
    workflow = _workflow()
    source = WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == [P9_BRANCH]
    assert "workflow_call" in workflow["on"]
    assert set(workflow["jobs"]) == {"lock_rewrite_analysis"}

    job = workflow["jobs"]["lock_rewrite_analysis"]
    env = job["env"]
    assert env["P9L_PREDECESSOR"] == PREDECESSOR
    assert env["P9L_HEAD"] == HEAD
    assert int(env["P9L_LOCK_TIMEOUT_MS"]) == LOCK_TIMEOUT_MS
    assert int(env["P9L_STATEMENT_TIMEOUT_MS"]) == STATEMENT_TIMEOUT_MS
    assert int(env["P9L_LOCK_WAIT_BUDGET_MS"]) == LOCK_WAIT_BUDGET_MS
    assert int(env["P9L_MIGRATION_DURATION_BUDGET_MS"]) == MIGRATION_DURATION_BUDGET_MS
    assert int(env["P9L_CONTROLLED_BLOCK_HOLD_MS"]) == CONTROLLED_BLOCK_HOLD_MS
    assert int(env["P9L_SAMPLE_INTERVAL_MS"]) == SAMPLE_INTERVAL_MS

    for token in (
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
        "postgresql-16",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "lock_timeout = '1500ms'",
        "statement_timeout = '15000ms'",
        'upgrade "${P9L_PREDECESSOR}"',
        "p9m_seed_populated_predecessor.sql",
        "p9m_stable_snapshot.sql",
        "p9l_lock_rewrite_probe.py",
        "p9m_verify_head_capability.sql",
        'cmp "$EVIDENCE_DIR/predecessor-stable.txt" "$EVIDENCE_DIR/head-stable.txt"',
        "tests/test_p9m_populated_migration_contracts.py",
        "tests/test_p8_governance_contracts.py",
        "tests/test_migration_app_secure_owner_context_boundary.py",
    ):
        assert token in source

    for marker in (
        "P9_LOCK_BUDGET=PASS",
        "P9_TABLE_REWRITE_ANALYSIS=PASS",
        "P9_MIGRATION_DURATION_MEASURED=PASS",
        "P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in source

    assert "alembic downgrade" not in source
    assert "ALLOW_DESTRUCTIVE_MIGRATIONS" not in source
    assert "production deployment" not in source.lower()


def test_p9l_acceptance_matrix_exposes_frozen_runtime_limits() -> None:
    source = ACCEPTANCE.read_text(encoding="utf-8")
    for phrase in (
        "P9-L frozen runtime budgets",
        "1,500 ms",
        "15,000 ms",
        "750 ms",
        "10,000 ms",
        "200 ms",
        "5 ms",
        "AccessExclusiveLock",
        "relfilenode",
    ):
        assert phrase in source
