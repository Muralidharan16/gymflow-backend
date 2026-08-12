from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("tests/test_branch_lifecycle.py")


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(_source(), filename=str(SOURCE))
    matches = [
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one lifecycle test/function named {name}, "
        f"found {len(matches)}"
    )
    return matches[0]


def _function_source(name: str) -> str:
    source = _source()
    node = _function_node(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _executable_string_literals(name: str) -> list[str]:
    """Return string literals from executable function body, excluding docstring."""
    node = _function_node(name)
    body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
    values: list[str] = []
    for statement in body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                values.append(child.value)
    return values


def test_lifecycle_fixture_seeds_tenant_root_only_through_admin_identity() -> None:
    source = _source()
    fixture = _function_source("lifecycle_setup")

    assert "from conftest import AdminTestSessionLocal, assert_test_database" in source
    assert "async with AdminTestSessionLocal() as session:" in fixture
    assert "Organization(id=org_id" in fixture
    assert "await set_db_session_context(session, str(org_id), str(owner_id), \"owner\")" in fixture
    assert "auth_db_session" in fixture
    assert "auth_db_session.add_all([b1, b2])" in fixture

    # The reduced production-equivalent API runtime must not become a fixture
    # backdoor for tenant-root or initial branch creation.
    assert "async with AsyncSessionLocal() as session:" not in fixture


def test_lifecycle_cleanup_does_not_invent_hard_delete_or_rls_bypass() -> None:
    cleanup = _function_source("cleanup_lifecycle_fixture")
    executable_literals = _executable_string_literals("cleanup_lifecycle_fixture")
    normalized_literals = [literal.upper() for literal in executable_literals]

    assert "async with AdminTestSessionLocal() as session:" in cleanup
    assert "await assert_test_database(session)" in cleanup
    assert "RESET ROLE" in cleanup
    assert "TRUNCATE TABLE" in cleanup

    # Lifecycle append/queue rows are test-owned evidence and may be cleared in
    # the disposable CI database. Tenant roots and branch state intentionally
    # have no production hard-delete capability, so teardown must never weaken
    # FORCE RLS or create a transient privilege just to remove fixture roots.
    for relation in (
        "branch_status_history",
        "branch_lifecycle_events",
        "branch_outbox_events",
        "branch_watchdog_alerts",
    ):
        assert relation in cleanup

    forbidden_fragments = (
        "CASCADE",
        "ALTER TABLE",
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "SET ROW_SECURITY",
        "BYPASSRLS",
        "GRANT DELETE",
        "DELETE FROM PUBLIC.ORG_BRANCH_STATE",
        "DELETE FROM PUBLIC.ORG_BRANCHES",
        "DELETE FROM PUBLIC.ORGANIZATIONS",
        "DELETE FROM PUBLIC.ORGANIZATION_USERS",
        "DELETE FROM PUBLIC.GYM_OWNERS",
    )
    for literal in normalized_literals:
        assert all(fragment not in literal for fragment in forbidden_fragments)


def test_tenant_lifecycle_behavior_still_executes_on_reduced_api_runtime() -> None:
    for test_name in (
        "test_initiate_transition_unauthorized_role",
        "test_initiate_transition_missing_reason_on_terminal",
        "test_initiate_transition_last_active_branch_guard",
        "test_saga_happy_path_and_transaction_b_failure_is_retry_safe",
    ):
        test_source = _function_source(test_name)
        assert "AsyncSessionLocal()" in test_source
        assert "maintenance_db_session" not in test_source
        assert "AdminTestSessionLocal()" not in test_source
        assert "run_watchdog_sweep" not in test_source
        assert "run_reconciliation_sweep" not in test_source


def test_watchdog_behavior_splits_tenant_transaction_from_maintenance_sweep() -> None:
    test_source = _function_source(
        "test_watchdog_alerts_without_compensating_retryable_saga"
    )

    # Transaction A is a tenant/API operation; the cross-tenant sweep is not.
    assert "AsyncSessionLocal()" in test_source
    assert "maintenance_db_session" in test_source
    assert "await set_maintenance_session_context(maintenance_db_session)" in test_source
    assert "maintenance_service = BranchLifecycleService(maintenance_db_session)" in test_source
    assert "await maintenance_service.run_watchdog_sweep()" in test_source
    assert "AdminTestSessionLocal()" not in test_source


def test_reconciliation_behavior_executes_only_on_maintenance_identity() -> None:
    test_source = _function_source(
        "test_reconciliation_sweep_releases_claim_and_advances_projection"
    )

    assert "maintenance_db_session" in test_source
    assert "await set_maintenance_session_context(maintenance_db_session)" in test_source
    assert "service = BranchLifecycleService(maintenance_db_session)" in test_source
    assert "await service.run_reconciliation_sweep()" in test_source
    assert "AsyncSessionLocal()" not in test_source
    assert "AdminTestSessionLocal()" not in test_source
