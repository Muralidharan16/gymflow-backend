from __future__ import annotations

import ast
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "app" / "core" / "supervisor.py"
MIGRATION = ROOT / "alembic" / "versions" / "b06c7d8e9f0a_tenant_dek_lookup_boundary.py"


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: pathlib.Path, function_name: str) -> str:
    source = _source(path)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            assert node.end_lineno is not None
            lines = source.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"missing function {function_name}")


def test_tenant_dek_lookup_is_a_bounded_security_definer_capability() -> None:
    source = _source(MIGRATION)
    assert 'revision = "b06c7d8e9f0a"' in source
    assert 'down_revision = "af5b6c7d8e9f"' in source
    assert "CREATE FUNCTION app_secure.lookup_encrypted_dek" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog" in source
    assert "SET row_security = on" in source
    assert "app.current_org_id" in source
    assert "IS DISTINCT FROM p_tenant_id::text" in source
    assert "public.encryption_key_registry" in source
    assert "REVOKE ALL ON FUNCTION app_secure.lookup_encrypted_dek(uuid,integer) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION app_secure.lookup_encrypted_dek(uuid,integer) TO app_runtime" in source


def test_api_runtime_never_receives_direct_key_registry_select() -> None:
    source = _source(MIGRATION)
    normalized = re.sub(r"\s+", " ", source).upper()
    assert (
        "GRANT SELECT (TENANT_ID, KEY_VERSION, ENCRYPTED_DEK) "
        "ON TABLE PUBLIC.ENCRYPTION_KEY_REGISTRY TO APP_SECURITY_OWNER"
    ) in normalized
    assert not re.search(
        r"GRANT\s+(?:SELECT|ALL).*?ON\s+(?:TABLE\s+)?PUBLIC\.ENCRYPTION_KEY_REGISTRY\s+TO\s+APP_RUNTIME",
        normalized,
        flags=re.DOTALL,
    )
    assert "GRANT ALL" not in normalized
    assert "rolbypassrls" in source.lower()
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+(?:APP_RUNTIME|APP_SECURITY_OWNER)\b[^;]*\bBYPASSRLS\b",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_supervisor_uses_tenant_context_before_bounded_dek_helper() -> None:
    source = _function_source(SUPERVISOR, "_lookup_encrypted_dek")
    assert "AsyncSessionLocal" in source
    assert "update_session_context(db, org_id=tenant_id)" in source
    assert "app_secure.lookup_encrypted_dek" in source
    assert "public.encryption_key_registry" not in source
    assert "SELECT encrypted_dek" not in source
    assert source.index("update_session_context(db, org_id=tenant_id)") < source.index("db.execute(")


def test_startup_registers_the_exact_certified_lookup() -> None:
    source = _function_source(SUPERVISOR, "_dek_registry_startup")
    assert "EnvelopeEncryptionProvider.register_dek_lookup(_lookup_encrypted_dek)" in source
    assert "AsyncSessionLocal" not in source
    assert "encryption_key_registry" not in source


def test_downgrade_removes_helper_and_exact_security_owner_columns() -> None:
    source = _function_source(MIGRATION, "downgrade")
    assert "_require_forward_contract(bind)" in source
    assert "DROP FUNCTION app_secure.lookup_encrypted_dek(uuid,integer)" in source
    assert "REVOKE SELECT (tenant_id, key_version, encrypted_dek)" in source
    assert "FROM app_security_owner" in source
    assert "CASCADE" not in source
    assert "_require_predecessor(bind)" in source
