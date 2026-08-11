from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "app/routers/branch_lifecycle.py"
SERVICE = ROOT / "app/services/branch_lifecycle_service.py"
POLLER = ROOT / "app/tasks/branch_outbox_poller.py"
MIGRATION = ROOT / "alembic/versions/2c3d4e5f6071_harden_branch_lifecycle_worker.py"
CHILD_MIGRATION = ROOT / "alembic/versions/3d4e5f607182_bound_lifecycle_child_commands.py"
RECLAIM_MIGRATION = ROOT / "alembic/versions/4e5f60718293_index_lifecycle_outbox_reclaims.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_http_transition_never_carries_request_session_into_background_work() -> None:
    source = _source(ROUTER)
    tree = ast.parse(source)

    assert "BackgroundTasks" not in source
    assert ".add_task(" not in source
    assert "execute_saga_cascade" not in source
    assert "branch.lifecycle_saga" in source
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "fastapi"
        and any(alias.name == "BackgroundTasks" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_transaction_a_persists_saga_intent_and_never_claims_external_delivery() -> None:
    source = _source(SERVICE)

    assert 'event_type="branch.lifecycle_saga"' in source
    assert 'event_type="branch.search_deindex"' in source
    assert 'else "branch.search_index"' in source
    assert "refunds_completed" not in source
    assert "refunds_initiated" not in source
    assert "notifications_sent" not in source
    assert 'event_type="branch.refund_required"' in source
    assert 'event_type="branch.member_notification"' in source
    assert '"refunds_queued"' in source
    assert '"notifications_queued"' in source


def test_transaction_b_is_caller_owned_and_atomic_for_worker_execution() -> None:
    source = _source(SERVICE)

    assert "parent_outbox_id" in source
    assert "worker_id" in source
    assert "enqueue_branch_lifecycle_child" in source
    assert 'event_type="saga_database_completed"' in source
    assert "if parent_outbox_id is None:" in source

    # No checkpoint helper may commit before the effect it represents. Direct
    # foreground/admin repair may commit only after the whole saga method.
    checkpoint_start = source.index("async def _record_checkpoint")
    checkpoint_end = source.index("async def _update_checkpoint", checkpoint_start)
    checkpoint_source = source[checkpoint_start:checkpoint_end]
    assert "await self.db.commit()" not in checkpoint_source
    assert "await self.db.flush()" in checkpoint_source


def test_worker_claims_reclaims_and_commits_parent_with_transaction_b() -> None:
    source = _source(POLLER)

    assert "worker_async_session_maker" in source
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "status = 'pending'" in source
    assert "status = 'processing'" in source
    assert "leased_until <= pg_catalog.clock_timestamp()" in source
    assert "attempt_count = outbox_data.attempt_count + 1" in source
    assert "execute_saga_cascade" in source
    assert "await _mark_delivered" in source
    assert "await session.commit()" in source
    assert "compensate_saga_from_dead_letter" in source
    assert "2 ** max(attempts - 1, 0)" in source


def test_external_commands_fail_closed_until_real_handlers_exist() -> None:
    source = _source(POLLER)

    assert "Logging is not delivery" in source
    assert "No production handler is configured" in source
    assert "branch.search_deindex" in source
    assert "branch.search_index" in source
    assert "branch.member_notification" in source
    assert "branch.refund_required" in source
    assert "mock" not in source.lower()


def test_worker_migration_is_lease_bound_and_owns_only_lifecycle_delta() -> None:
    source = _source(MIGRATION)

    assert "worker_runtime" in source
    assert "worker_runtime member" in source.lower()
    assert "branch.lifecycle_saga" in source
    assert "leased_by" in source
    assert "leased_until" in source
    assert "app.worker_id" in source
    assert "app.internal_maintenance" in source
    assert "branch_lifecycle_saga" in source
    assert "must never revoke" in source
    assert "GRANT UPDATE (status, is_operational, lifecycle_transition_in_progress" in source
    assert "GRANT UPDATE ON TABLE public.org_branch_state TO worker_runtime" not in source
    assert "GRANT SELECT ON TABLE public.organizations TO worker_runtime" not in source
    assert "GRANT UPDATE ON TABLE public.organizations TO worker_runtime" not in source
    assert "BYPASSRLS" not in source.replace("NOBYPASSRLS", "")


def test_final_lifecycle_child_boundary_removes_direct_worker_insert() -> None:
    source = _source(CHILD_MIGRATION)

    assert "DROP POLICY lifecycle_worker_outbox_insert" in source
    assert "REVOKE INSERT ON TABLE public.branch_outbox_events FROM worker_runtime" in source
    assert "CREATE FUNCTION public.enqueue_branch_lifecycle_child" in source
    assert "SECURITY DEFINER" in source
    assert "SET row_security = on" in source
    assert "FROM PUBLIC" in source
    assert "TO worker_runtime" in source
    assert "live owned saga lease" in source
    assert "tenant/branch lineage mismatch" in source


def test_lifecycle_claim_index_includes_expired_processing_recovery() -> None:
    source = _source(RECLAIM_MIGRATION)

    assert "status IN ('pending', 'processing')" in source
    assert "leased_until" in source
    assert "process_after" in source
