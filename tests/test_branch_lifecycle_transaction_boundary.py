from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("app/services/branch_lifecycle_service.py")
SERVICE_CLASS = "BranchLifecycleService"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _method_source(name: str) -> str:
    """Return one concrete BranchLifecycleService method from source.

    The lifecycle API is class-based. Resolve methods from the named service
    class rather than scanning only module-level functions (which misses every
    method) or recursively matching any same-named nested function (which could
    let an unrelated helper satisfy a security/transaction contract).
    """
    source = _source()
    module = ast.parse(source, filename=str(SOURCE))
    service = next(
        item
        for item in module.body
        if isinstance(item, ast.ClassDef) and item.name == SERVICE_CLASS
    )
    matches = [
        item
        for item in service.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {SERVICE_CLASS}.{name} method, found {len(matches)}"
    )
    node = matches[0]
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_transition_authorizes_from_plain_read_before_write_locking() -> None:
    transition = _method_source("initiate_transition")

    visible_read = transition.index("visible_stmt = select(OrgBranchState).where(")
    authorization = transition.index("if actor_role not in allowed_trans.allowed_roles:")
    advisory_lock = transition.index("SELECT pg_try_advisory_xact_lock(")
    write_lock = transition.index(".with_for_update()")

    assert visible_read < authorization < advisory_lock < write_lock
    assert "status_code=status.HTTP_403_FORBIDDEN" in transition
    assert "branch_state.status != from_status" in transition
    assert "Branch status changed while the transition was being authorized" in transition


def test_optional_booking_surface_is_checked_before_update_without_swallowing_errors() -> None:
    relation_exists = _method_source("_relation_exists")
    cancel = _method_source("_cancel_future_bookings_if_present")
    saga = _method_source("execute_saga_cascade")

    assert "pg_catalog.to_regclass(:qualified_name) IS NOT NULL" in relation_exists
    assert 'if not await self._relation_exists("public.bookings")' in cancel
    assert "UPDATE public.bookings" in cancel

    # Missing optional infrastructure is handled before DML. Once the table is
    # present, SQL/ACL/data failures are real saga failures and must reach the
    # outer rollback+compensation path instead of being hidden inside a nested
    # broad catch that leaves PostgreSQL in failed-transaction state.
    assert "except Exception" not in cancel
    assert "_cancel_future_bookings_if_present" in saga
    assert "await self.db.rollback()" in saga
    assert "await self._compensate_saga" in saga


def test_reconciliation_isolates_per_branch_failures_with_savepoints() -> None:
    reconcile = _method_source("run_reconciliation_sweep")

    assert "async with self.db.begin_nested():" in reconcile
    assert "update_result.rowcount != 1" in reconcile
    assert "clear_result.rowcount != 1" in reconcile
    assert "search_sync_failed_at = :now" in reconcile
    assert "return synced_count" in reconcile
    assert "return len(claimed_ids)" not in reconcile


def test_watchdog_refuses_missing_transition_timestamp_instead_of_crashing_math() -> None:
    watchdog = _method_source("run_watchdog_sweep")

    assert "if changed_at is None:" in watchdog
    assert "manual review required" in watchdog
    assert "continue" in watchdog
