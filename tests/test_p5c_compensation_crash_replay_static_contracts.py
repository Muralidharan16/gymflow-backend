from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/architecture/P5C_COMPENSATION_CRASH_REPLAY.md"
RUNTIME = ROOT / "tests/test_p5c_compensation_crash_replay_runtime.py"
WORKFLOW = ROOT / ".github/workflows/p5c-compensation-crash-replay-pg16.yml"
POLLER = ROOT / "app/tasks/branch_outbox_poller.py"
LIFECYCLE = ROOT / "app/services/branch_lifecycle_service.py"
CELERY = ROOT / "app/core/celery_app.py"


def test_certified_p5r_predecessor_is_recorded_exactly() -> None:
    source = DOC.read_text(encoding="utf-8")
    for phrase in (
        "49877b224fd987b6f1d90b78db96bdc4074aa2aa",
        "7fc97c9a76c06641140036e0b89ef521e164ade3",
        "34817614212",
        "103891609964",
        "zj07d8e9f0a44",
        "79 passed",
        "6 passed",
        "91 passed",
    ):
        assert phrase in source


def test_scope_freezes_both_compensation_crash_boundaries_and_cardinality() -> None:
    source = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "process death before compensation commit",
        "process death after compensation commit before task acknowledgement",
        "one `compensation_completed` lifecycle event",
        "one `branchstatushistory` row",
        "one deterministic `branch.search_index` compensation child",
        "no frozen unrecoverable aggregate",
        "sigkill",
        "replacement worker",
        "lease_fence",
        "refund-provider execution remains deferred and fail-closed",
    ):
        assert phrase in source


def test_production_compensation_and_parent_deadletter_share_one_commit() -> None:
    source = POLLER.read_text(encoding="utf-8")
    start = source.index("async def _fail_event")
    end = source.index("\n\nasync def _process_deferred_external_event", start)
    method = source[start:end]

    compensate = method.index("await service.compensate_saga_from_dead_letter(")
    parent_terminal = method.index("SET status = 'dead_lettered'", compensate)
    commit = method.index("await session.commit()", parent_terminal)
    success = method.index('return "dead_lettered_compensated"', commit)

    assert compensate < parent_terminal < commit < success
    assert "AND leased_by = :worker_id" in method
    assert "AND lease_fence = :lease_fence" in method
    assert "AND leased_until > pg_catalog.clock_timestamp()" in method


def test_compensation_is_idempotent_and_child_identity_is_deterministic() -> None:
    source = LIFECYCLE.read_text(encoding="utf-8")
    start = source.index("async def compensate_saga_from_dead_letter")
    end = source.index("\n    async def run_watchdog_sweep", start)
    method = source[start:end]

    assert "if not state.lifecycle_transition_in_progress:" in method
    assert "return" in method
    assert 'state.transition_source = "saga_compensation"' in method
    assert 'event_type="compensation_completed"' in method
    assert 'transition_source="saga_compensation"' in method
    assert 'event_type="branch.search_index"' in method

    enqueue_start = source.index("async def _enqueue_child_command")
    enqueue_end = source.index("\n    async def execute_saga_cascade", enqueue_start)
    enqueue = source[enqueue_start:enqueue_end]
    assert "uuid.uuid5(" in enqueue
    assert 'f"{event_type}:{branch_state.branch_id}"' in enqueue


def test_runtime_uses_real_process_death_and_database_barrier() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    for phrase in (
        'P5C_PROCESS_FAULTS") != "1"',
        'P5C_DISPOSABLE_DATABASE") != _DATABASE',
        "start_new_session=True",
        "signal.SIGKILL",
        "os.killpg",
        "pg_stat_activity",
        "p5c_compensation_precommit_barrier",
        "_expire_killed_lease",
        "_claim_specific",
        "lease_fence",
        "dead_lettered_compensated",
        "compensation_completed",
        "saga_compensation",
        "branch.search_index",
    ):
        assert phrase in source
    for forbidden in ("monkeypatch", "Mock(", "AsyncMock"):
        assert forbidden not in source


def test_runtime_names_both_decisive_crash_scenarios() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "test_precommit_process_death_rolls_back_and_replacement_worker_recovers" in source
    assert "test_after_commit_process_death_redelivery_is_single_effect" in source


def test_canonical_celery_ack_contract_remains_late_and_worker_lost_safe() -> None:
    source = CELERY.read_text(encoding="utf-8")
    assert "task_acks_late=True" in source
    assert "task_reject_on_worker_lost=True" in source
    assert "worker_prefetch_multiplier=1" in source


def test_workflow_is_pg16_reduced_identity_exact_head_and_same_sha() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(source)
    assert isinstance(parsed, dict)
    for phrase in (
        "ubuntu-24.04",
        "redis:7-alpine",
        "scripts/ci/install_pg16_test_stack.sh",
        "scripts/ci/bootstrap_cluster_roles.sh",
        "auth_p5c_runtime",
        "app_test_runtime",
        "worker_test_runtime",
        "NOBYPASSRLS",
        "zj07d8e9f0a44",
        "tests/test_p5c_compensation_crash_replay_runtime.py",
        "tests/test_p5c_compensation_crash_replay_static_contracts.py",
        'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"',
        "P5C_BEFORE_COMMIT_CRASH=PASS",
        "P5C_REPLACEMENT_WORKER_RECOVERY=PASS",
        "P5C_AFTER_COMMIT_REDELIVERY=PASS",
        "P5C_SINGLE_COMPENSATION_EFFECT=PASS",
        "P5C_NO_FROZEN_UNRECOVERABLE_AGGREGATE=PASS",
    ):
        assert phrase in source
    assert "continue-on-error" not in source
    assert "|| true" not in source


def test_slice_does_not_broaden_privilege_or_later_phase_scope() -> None:
    executable = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (RUNTIME, WORKFLOW)
    )
    for forbidden in (
        "grant all on",
        "alter role app_test_runtime superuser",
        "alter role worker_test_runtime superuser",
        "p5-f certified",
        "refund provider execution",
    ):
        assert forbidden not in executable
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "NOBYPASSRLS" in workflow
    assert " BYPASSRLS" not in workflow.replace("NOBYPASSRLS", "")
