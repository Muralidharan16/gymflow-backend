from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/c47d8e9f0a1e_p3a_auth_runtime_decoupling.py"
BINDINGS = ROOT / "security/runtime_identity/runtime_bindings.v1.json"
AUTH_DATABASE = ROOT / "app/core/auth_database.py"


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _migration_source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_c47_is_downstream_of_principal_binding() -> None:
    source = _migration_source()
    assert 'revision = "c47d8e9f0a1e"' in source
    assert 'down_revision = "c37d8e9f0a1d"' in source


def test_auth_binding_does_not_inherit_ordinary_api_runtime() -> None:
    manifest = json.loads(BINDINGS.read_text(encoding="utf-8"))
    auth = manifest["bindings"]["auth"]
    assert auth["runtime_capability"] == "auth_runtime"
    assert auth["direct_capabilities"] == ["auth_runtime", "app_user"]
    assert "app_runtime" not in auth["direct_capabilities"]


def test_auth_address_authority_is_exact_and_force_rls_guarded() -> None:
    source = _migration_source()
    normalized = " ".join(source.split())
    assert '_ALLOWED_AUTH_ADDRESS = {"SELECT", "INSERT"}' in source
    assert "organization_addresses must retain ENABLE + FORCE RLS" in source
    assert "tenant_isolation_addr_select" in source
    assert "tenant_isolation_addr_insert" in source
    assert (
        "GRANT SELECT, INSERT ON TABLE public.organization_addresses TO auth_runtime"
        in normalized
    )
    assert (
        "REVOKE SELECT, INSERT ON TABLE public.organization_addresses FROM auth_runtime"
        in normalized
    )

    # Restrict privilege-mutation assertions to executable migration entrypoints;
    # comments and catalog column names such as rolbypassrls are not mutations.
    executable = (_function_source("upgrade") + _function_source("downgrade")).upper()
    for forbidden in (
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT TRUNCATE",
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
        "GRANT ALL",
    ):
        assert forbidden not in executable


def test_auth_role_has_no_direct_profile_function_execute() -> None:
    source = _migration_source()
    assert "_require_profile_function_separation(bind)" in source
    assert "auth_runtime has direct ordinary profile EXECUTE" in source
    assert "app_secure.current_organization_profile()" in source
    assert "app_secure.update_current_organization_profile(jsonb)" in source


def test_auth_dependency_requires_fastapi_request_injection() -> None:
    source = AUTH_DATABASE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(AUTH_DATABASE))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_auth_db"
    )
    request_arg = fn.args.args[0]
    assert request_arg.arg == "request"
    assert isinstance(request_arg.annotation, ast.Name)
    assert request_arg.annotation.id == "Request"
    assert fn.args.defaults == []
    assert "await initialize_request_session(session, request)" in source
