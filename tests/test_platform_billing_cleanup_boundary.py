from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests/platform_billing/test_phase1_schema.py"


def _module() -> ast.Module:
    return ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))


def _assignment(name: str) -> ast.AST:
    for node in _module().body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"missing assignment: {name}")


def _async_function(name: str) -> ast.AsyncFunctionDef:
    for node in _module().body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function: {name}")


def _string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def test_platform_billing_cleanup_never_truncates_organizations_or_cascades():
    phase1 = _assignment("PHASE_1_TABLES")
    assert isinstance(phase1, ast.List)
    phase1_names = {
        item.value
        for item in phase1.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    assert "organizations" not in phase1_names

    cleanup = _async_function("cleanup_phase1_tables")
    strings = _string_constants(cleanup)
    truncate_strings = [value for value in strings if "TRUNCATE" in value.upper()]

    assert truncate_strings, "cleanup must explicitly clear Platform Billing tables"
    assert all("ORGANIZATIONS" not in value.upper() for value in truncate_strings)
    assert all("CASCADE" not in value.upper() for value in truncate_strings)


def test_platform_billing_cleanup_deletes_only_declared_test_organizations():
    test_org_ids = _assignment("TEST_ORGANIZATION_IDS")
    assert isinstance(test_org_ids, ast.Tuple)
    assert [item.id for item in test_org_ids.elts if isinstance(item, ast.Name)] == ["ORG_1", "ORG_2"]

    cleanup = _async_function("cleanup_phase1_tables")
    strings = _string_constants(cleanup)
    normalized = [" ".join(value.split()).upper() for value in strings]

    assert any(
        value == "DELETE FROM ORGANIZATIONS WHERE ID = ANY(:ORGANIZATION_IDS)"
        for value in normalized
    )
    assert any(
        value == "SELECT COUNT(*) FROM ORGANIZATIONS WHERE ID = ANY(:ORGANIZATION_IDS)"
        for value in normalized
    )


def test_platform_billing_cleanup_has_no_rls_or_role_bypass():
    cleanup = _async_function("cleanup_phase1_tables")
    strings = "\n".join(_string_constants(cleanup)).upper()

    assert "ROW_SECURITY = OFF" not in strings
    assert "DISABLE ROW LEVEL SECURITY" not in strings
    assert "SET ROLE POSTGRES" not in strings
    assert "SET ROLE MIGRATION_OWNER" not in strings


def test_platform_billing_rls_uses_configured_runtime_identity_without_role_switch():
    rls_test = _async_function("test_tenant_rls_and_composite_foreign_keys")
    strings = "\n".join(_string_constants(rls_test)).upper()

    assert "SELECT CURRENT_USER" in strings
    assert "SET ROLE APP_RUNTIME" not in strings
    assert "SET ROLE MIGRATION_OWNER" not in strings
    assert "ROW_SECURITY = OFF" not in strings

    calls_config = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_platform_billing_test_config"
        for node in ast.walk(rls_test)
    )
    assert calls_config

    compares_runtime_user = any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "config"
        and node.attr == "runtime_user"
        for node in ast.walk(rls_test)
    )
    assert compares_runtime_user
