from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/branch_lifecycle_service.py"
MIGRATION = (
    ROOT
    / "alembic/versions/a4b5c6d7e8f9_bound_lifecycle_compensation_metadata.py"
)
VERIFIER = ROOT / "scripts/verify_lifecycle_retry_exhaustion.py"
WORKFLOW = ROOT / ".github/workflows/lifecycle-compensation-production-boundary.yml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_compensation_state_metadata_is_complete_and_fail_closed() -> None:
    source = _source(SERVICE)
    start = source.index("async def compensate_saga_from_dead_letter")
    end = source.index("async def run_watchdog_sweep", start)
    compensation = source[start:end]

    assert "state.status != to_status" in compensation
    assert 'state.saga_compensation_strategy != "rollback_to_origin"' in compensation
    assert "state.saga_last_checkpoint is not None" in compensation
    assert "state.status_changed_by != actor_id" in compensation
    assert "state.status_changed_at = now" in compensation
    assert "state.status_reason = compensation_reason" in compensation
    assert 'state.transition_source = "saga_compensation"' in compensation
    assert 'event_type="compensation_completed"' in compensation
    assert 'transition_source="saga_compensation"' in compensation


def test_transaction_a_state_metadata_matches_immutable_history() -> None:
    source = _source(SERVICE)
    start = source.index("async def initiate_transition")
    end = source.index("async def _record_checkpoint", start)
    transaction_a = source[start:end]

    assert "branch_state.status_changed_at = now" in transaction_a
    assert "branch_state.status_changed_by = actor_id" in transaction_a
    assert "branch_state.status_reason =" in transaction_a
    assert "branch_state.transition_source = transition_source" in transaction_a
    assert "transition_source=transition_source" in transaction_a


def test_compensation_metadata_privilege_delta_is_column_scoped_and_reversible() -> None:
    source = _source(MIGRATION)

    assert 'down_revision = "93a4b5c6d7e8"' in source
    assert '"status_changed_at"' in source
    assert '"status_reason"' in source
    assert '"transition_source"' in source
    assert (
        "GRANT UPDATE (status_changed_at, status_reason, transition_source) "
        in source
    )
    assert (
        "REVOKE UPDATE (status_changed_at, status_reason, transition_source) "
        in source
    )
    assert "has_table_privilege(:worker, :relation, 'UPDATE')" in source
    assert "lifecycle_worker_state_update" in source
    assert "relforcerowsecurity" in source
    assert "GRANT UPDATE ON TABLE public.org_branch_state TO worker_runtime" not in source
    assert "BYPASSRLS" not in source.replace("NOBYPASSRLS", "")


def test_real_pg16_compensation_gate_is_part_of_pr_ci() -> None:
    verifier = _source(VERIFIER)
    workflow = _source(WORKFLOW)

    assert "wrong_outcome == \"lease_lost\"" in verifier
    assert 'outcome == "dead_lettered_compensated"' in verifier
    assert 'replay_outcome == "lease_lost"' in verifier
    assert "compensation_events == 1" in verifier
    assert "compensation_history == 1" in verifier
    assert "compensation_search == 1" in verifier
    assert "Fresh PG16 lineage to HEAD" in workflow
    assert "Prove retry exhaustion compensation on real worker identity" in workflow
