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

    # The integration path must use the same centralized context API as runtime
    # requests, not define a test-only helper that duplicates raw PostgreSQL GUCs.
    assert "update_session_context" in source
    assert "set_tenant_context" not in source
    assert "pg_catalog.set_config" not in source

    assert "principal_id=str(owner_id)" in source
    assert "principal_type='legacy_gym_owner'" in source
    assert "org_id=str(org_id)" in source


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

    # One org-only setup and one typed-actor upgrade are sufficient. Subsequent
    # commits must rely on the Session.after_begin hook rather than manually
    # replaying tenant context for every transaction.
    assert len(context_calls) == 2
    assert source.count("await db.commit()") >= 4

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
