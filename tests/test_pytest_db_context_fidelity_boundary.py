from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


CONFTEST = Path("tests/conftest.py")
PRODUCTION_DB = Path("app/core/database.py")
AUTH_DB = Path("app/core/auth_database.py")


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _async_function(path: Path, name: str) -> ast.AsyncFunctionDef:
    for node in _module(path).body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function {name} in {path}")


def _sync_function(path: Path, name: str) -> ast.FunctionDef:
    for node in _module(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name} in {path}")


def _source_segment(path: Path, node: ast.AST) -> str:
    source = path.read_text(encoding="utf-8")
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def _load_test_url_validator():
    node = _sync_function(CONFTEST, "validate_test_database_url")
    namespace = {"make_url": make_url}
    exec(_source_segment(CONFTEST, node), namespace)
    return namespace["validate_test_database_url"]


def _state_getattr_calls(function: ast.AsyncFunctionDef) -> dict[str, list[ast.Call]]:
    result: dict[str, list[ast.Call]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
            continue
        if len(node.args) < 2:
            continue
        if not isinstance(node.args[0], ast.Name) or node.args[0].id != "state":
            continue
        field = node.args[1]
        if not isinstance(field, ast.Constant) or not isinstance(field.value, str):
            continue
        result.setdefault(field.value, []).append(node)
    return result


def _constant(node: ast.AST, expected) -> bool:
    return isinstance(node, ast.Constant) and node.value == expected


def _calls_named(function: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _calls_initializer(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "initialize"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "SessionContextInitializer"
        for node in ast.walk(function)
    )


def test_pytest_db_override_requires_fastapi_request_injection() -> None:
    override = _async_function(CONFTEST, "override_get_db")
    assert len(override.args.args) == 1
    request_arg = override.args.args[0]
    assert request_arg.arg == "request"
    assert isinstance(request_arg.annotation, ast.Name)
    assert request_arg.annotation.id == "Request"
    assert override.args.defaults == []


def test_production_context_initializer_is_the_single_request_state_contract() -> None:
    initializer = _async_function(PRODUCTION_DB, "initialize_request_session")
    getattrs = _state_getattr_calls(initializer)

    assert {
        "principal_id",
        "staff_id",
        "principal_type",
        "org_id",
        "gym_id",
        "role",
        "otel_trace_id",
        "correlation_id",
    } <= set(getattrs)

    staff = getattrs["staff_id"][0]
    assert len(staff.args) == 3
    assert isinstance(staff.args[2], ast.Name)
    assert staff.args[2].id == "principal_id"

    principal = getattrs["principal_id"][0]
    assert len(principal.args) == 3
    assert isinstance(principal.args[2], ast.Call)
    assert principal.args[2] is staff

    for field in ("principal_type", "org_id", "gym_id", "otel_trace_id"):
        assert any(len(call.args) == 3 and _constant(call.args[2], None) for call in getattrs[field])
    assert any(len(call.args) == 3 and _constant(call.args[2], "unknown") for call in getattrs["role"])
    assert any(
        len(call.args) == 3 and _constant(call.args[2], "unknown")
        for call in getattrs["correlation_id"]
    )
    assert _calls_initializer(initializer)

    for path, name in ((PRODUCTION_DB, "get_db"), (AUTH_DB, "get_auth_db")):
        function = _async_function(path, name)
        calls = _calls_named(function, "initialize_request_session")
        assert len(calls) == 1
        assert len(calls[0].args) == 2
        assert all(isinstance(arg, ast.Name) for arg in calls[0].args)
        assert [arg.id for arg in calls[0].args] == ["session", "request"]


def test_pytest_override_preserves_same_typed_context_fields() -> None:
    production = _async_function(PRODUCTION_DB, "initialize_request_session")
    override = _async_function(CONFTEST, "override_get_db")

    production_fields = set(_state_getattr_calls(production))
    override_fields = set(_state_getattr_calls(override))
    for field in ("org_id", "gym_id", "role", "otel_trace_id", "principal_type"):
        assert field in production_fields
        assert field in override_fields

    assert _calls_initializer(production)
    assert _calls_initializer(override)


def test_pytest_db_override_does_not_bypass_rls_or_impersonate_privileged_roles() -> None:
    override = _source_segment(
        CONFTEST,
        _async_function(CONFTEST, "override_get_db"),
    ).upper()
    forbidden = (
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "SET ROLE APP_RLS_EXECUTOR",
        "SET ROLE APP_SECURITY_OWNER",
        "SET SESSION AUTHORIZATION",
        "ROW_SECURITY = OFF",
    )
    for token in forbidden:
        assert token not in override


def test_test_url_validator_allows_same_disposable_db_with_distinct_identity() -> None:
    validate = _load_test_url_validator()
    runtime = (
        "postgresql+asyncpg://finance_test_runtime:runtime@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    migration = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    assert validate(runtime, migration) == runtime


def test_test_url_validator_rejects_same_identity_on_shared_disposable_db() -> None:
    validate = _load_test_url_validator()
    runtime = (
        "postgresql+asyncpg://migration_owner:runtime@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    migration = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_finance_test_ci"
    )
    with pytest.raises(RuntimeError, match="distinct runtime identity"):
        validate(runtime, migration)


def test_test_url_validator_rejects_exact_database_url_reuse() -> None:
    validate = _load_test_url_validator()
    url = (
        "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/"
        "gymflow_test"
    )
    with pytest.raises(RuntimeError, match="exact DATABASE_URL reuse"):
        validate(url, url)


def test_test_url_validator_still_rejects_non_test_database() -> None:
    validate = _load_test_url_validator()
    runtime = "postgresql+asyncpg://app_runtime:runtime@127.0.0.1:5432/gymflow"
    migration = "postgresql+asyncpg://migration_owner:admin@127.0.0.1:5432/gymflow"
    with pytest.raises(RuntimeError, match="must contain 'test'"):
        validate(runtime, migration)
