from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests/test_address.py"
DATABASE = ROOT / "app/core/database.py"
TEST_NAME = "test_audit_log_captured_on_update"


def _test_function() -> ast.AsyncFunctionDef:
    module = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == TEST_NAME:
            return node
    raise AssertionError(f"missing integration test: {TEST_NAME}")


def _string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def test_address_audit_integration_uses_session_owned_typed_context():
    test = _test_function()
    source = ast.unparse(test)

    # Both fixture administration and runtime behavior must use the same
    # centralized typed context API; neither may duplicate raw PostgreSQL GUCs.
    assert "update_session_context" in source
    assert "set_tenant_context" not in source
    assert "pg_catalog.set_config" not in source

    assert "principal_id=str(owner_id)" in source
    assert "principal_type='legacy_gym_owner'" in source
    assert "org_id=str(org_id)" in source


def test_address_audit_fixture_seed_respects_forced_rls_and_runtime_is_reduced():
    test = _test_function()
    source = ast.unparse(test)

    assert any(arg.arg == "admin_db_session" for arg in test.args.args)
    for required_seed in (
        "admin_db_session.add(Organization",
        "admin_db_session.add(branch)",
        "admin_db_session.add(branch_state)",
        "admin_db_session.add(owner)",
    ):
        assert required_seed in source

    first_admin_commit = source.index("await admin_db_session.commit()")
    admin_context = source.index("await update_session_context(admin_db_session")
    branch_seed = source.index("admin_db_session.add(branch)")
    state_seed = source.index("admin_db_session.add(branch_state)")
    assert first_admin_commit < admin_context < branch_seed < state_seed
    assert source.count("await admin_db_session.commit()") == 2
    assert "async with AsyncSessionLocal() as db" in source

    # Tenant-scoped branch rows are seeded by the administrative identity only
    # after typed tenant context is attached, so FORCE RLS remains authoritative.
    # Address writes and audit reads then execute under reduced runtime.
    runtime_source = source[source.index("async with AsyncSessionLocal() as db") :]
    assert "admin_db_session" not in runtime_source
    assert "db.add(branch)" not in runtime_source
    assert "db.add(branch_state)" not in runtime_source
    assert "db.add(owner)" not in runtime_source
    assert "db.add(addr)" in runtime_source


def test_address_audit_context_survives_commit_without_manual_reapplication():
    test = _test_function()
    source = ast.unparse(test)

    context_calls = [
        node
        for node in ast.walk(test)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "update_session_context"
    ]

    # One explicit context belongs to the FORCE-RLS fixture seed and one belongs
    # to reduced runtime behavior. Each session must then rely on after_begin to
    # reapply transaction-local context after commits.
    assert len(context_calls) == 2
    call_sessions = {
        ast.unparse(node.value.args[0])
        for node in context_calls
        if node.value.args
    }
    assert call_sessions == {"admin_db_session", "db"}
    assert source.count("await db.commit()") >= 2

    database_source = DATABASE.read_text(encoding="utf-8")
    assert '@event.listens_for(Session, "after_begin")' in database_source
    assert "_apply_context_sync(connection, context)" in database_source
    assert "pg_catalog.set_config(:setting_name, :setting_value, true)" in database_source


def test_address_audit_integration_does_not_bypass_rls():
    test = _test_function()
    sql = "\n".join(_string_constants(test)).upper()

    assert "ROW_SECURITY = OFF" not in sql
    assert "DISABLE ROW LEVEL SECURITY" not in sql
    assert "SET ROLE POSTGRES" not in sql
    assert "SET ROLE MIGRATION_OWNER" not in sql
    assert "BYPASSRLS" not in sql
