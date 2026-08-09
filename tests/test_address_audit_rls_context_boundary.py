from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests/test_address.py"
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


def test_address_audit_integration_establishes_tenant_context_per_transaction():
    test = _test_function()
    helpers = [
        node
        for node in ast.walk(test)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_tenant_context"
    ]
    assert len(helpers) == 1

    helper_strings = "\n".join(_string_constants(helpers[0]))
    assert "pg_catalog.set_config('app.current_org_id'" in helper_strings

    calls = [
        node
        for node in ast.walk(test)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "set_tenant_context"
    ]
    # branch/state insert, address insert, address update, audit read
    assert len(calls) >= 4


def test_address_audit_integration_does_not_bypass_rls():
    test = _test_function()
    sql = "\n".join(_string_constants(test)).upper()

    assert "ROW_SECURITY = OFF" not in sql
    assert "DISABLE ROW LEVEL SECURITY" not in sql
    assert "SET ROLE POSTGRES" not in sql
    assert "SET ROLE MIGRATION_OWNER" not in sql
    assert "BYPASSRLS" not in sql
