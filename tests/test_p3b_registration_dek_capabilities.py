from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/e07d8e9f0a25_p3b_registration_dek_capabilities.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])


def test_dek_capability_revision_follows_secure_storage() -> None:
    source = _source()
    assert 'revision = "e07d8e9f0a25"' in source
    assert 'down_revision = "d07d8e9f0a24"' in source
    assert '_KEY_SCOPE = "organization_registrations"' in source


def test_runtime_gets_execute_only_not_direct_key_material_acl() -> None:
    source = _source()
    upper = re.sub(r"\s+", " ", source).upper()

    assert "GRANT EXECUTE ON FUNCTION APP_SECURE.CURRENT_REGISTRATION_DEK() TO APP_RUNTIME" in upper
    assert "GRANT EXECUTE ON FUNCTION APP_SECURE.LOOKUP_REGISTRATION_DEK(INTEGER) TO APP_RUNTIME" in upper
    assert "GRANT EXECUTE ON FUNCTION APP_SECURE.INSTALL_REGISTRATION_DEK(BYTEA) TO APP_RUNTIME" in upper

    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|TRUNCATE|ALL).*?"
        r"ENCRYPTION_KEY_REGISTRY.*?TO\s+APP_RUNTIME",
        upper,
        flags=re.DOTALL,
    )
    assert not re.search(
        r"GRANT\s+(?:USAGE|SELECT|UPDATE|ALL).*?"
        r"ENCRYPTION_KEY_REGISTRY_KEY_VERSION_SEQ.*?TO\s+APP_RUNTIME",
        upper,
        flags=re.DOTALL,
    )
    assert "GRANT ALL" not in upper
    assert "BYPASSRLS" not in upper
    assert "OWNER TO APP_RUNTIME" not in upper


def test_security_owner_key_acl_is_narrow_and_reversible() -> None:
    source = _source()
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "GRANT SELECT (table_name, key_status)," in upgrade
    assert "INSERT (tenant_id, table_name, encrypted_dek, key_status)" in upgrade
    assert "TO app_security_owner" in upgrade
    assert "GRANT USAGE ON SEQUENCE public.encryption_key_registry_key_version_seq" in upgrade

    for forbidden in (
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT TRUNCATE",
        "GRANT SELECT ON TABLE public.encryption_key_registry",
        "GRANT INSERT ON TABLE public.encryption_key_registry",
        "GRANT SELECT ON SEQUENCE",
        "GRANT UPDATE ON SEQUENCE",
    ):
        assert forbidden not in upgrade

    assert "REVOKE USAGE ON SEQUENCE public.encryption_key_registry_key_version_seq" in downgrade
    assert "REVOKE SELECT (table_name, key_status)," in downgrade
    assert "INSERT (tenant_id, table_name, encrypted_dek, key_status)" in downgrade
    assert "_require_predecessor(bind)" in downgrade


def test_all_dek_capabilities_are_principal_bound_and_domain_bound() -> None:
    source = _source()
    guard = _function_source("_principal_guard_sql")

    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "('owner', 'admin')",
        "public.owners",
        "public.organization_users",
        "ERRCODE = '42501'",
    ):
        assert token in guard

    for sql_name in ("_CURRENT_FUNCTION", "_LOOKUP_FUNCTION", "_INSTALL_FUNCTION"):
        assert sql_name in source
    assert source.count("table_name = '{_KEY_SCOPE}'") >= 4
    assert source.count("SECURITY DEFINER") == 3
    assert source.count("SET search_path = pg_catalog") == 3
    assert source.count("SET row_security = on") == 3


def test_first_key_installation_is_database_serialized_and_race_safe() -> None:
    source = _source()
    assert "pg_catalog.pg_advisory_xact_lock" in source
    assert "pg_catalog.hashtextextended" in source
    assert "v_org_id::text || ':{_KEY_SCOPE}'" in source
    assert "AND key_data.key_status = 'ACTIVE'" in source
    assert "WHEN unique_violation THEN" in source
    assert "IF NOT FOUND THEN" in source
    assert "RAISE;" in source


def test_installer_rejects_empty_ciphertext_before_key_registry_write() -> None:
    source = _source()
    install_sql = source.split('_INSTALL_FUNCTION = f"""', 1)[1].split('"""\n\n\ndef _require_predecessor', 1)[0]

    validation = "IF p_encrypted_dek IS NULL OR pg_catalog.octet_length(p_encrypted_dek) = 0"
    assert validation in install_sql
    assert "ERRCODE = '22023'" in install_sql
    assert install_sql.index(validation) < install_sql.index("INSERT INTO public.encryption_key_registry")


def test_historical_lookup_requires_current_tenant_and_registration_scope() -> None:
    source = _source()
    lookup_sql = source.split('_LOOKUP_FUNCTION = f"""', 1)[1].split('"""\n\n_INSTALL_FUNCTION', 1)[0]

    assert "p_key_version IS NULL OR p_key_version < 1" in lookup_sql
    assert "key_data.tenant_id = v_org_id" in lookup_sql
    assert "key_data.table_name = '{_KEY_SCOPE}'" in lookup_sql
    assert "key_data.key_version = p_key_version" in lookup_sql
    assert "RETURN v_encrypted_dek" in lookup_sql


def test_function_installation_uses_temporary_schema_create_only() -> None:
    install = _function_source("_install_functions")

    assert "has_schema_privilege" in install
    assert "GRANT CREATE ON SCHEMA app_secure TO app_security_owner" in install
    assert "SET LOCAL ROLE app_security_owner" in install
    assert "RESET ROLE" in install
    assert "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner" in install
    assert install.index("RESET ROLE") < install.index(
        "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner"
    )


def test_forward_attestation_pins_exact_column_and_sequence_acl() -> None:
    source = _source()
    forward = _function_source("_require_forward")

    assert "_FORWARD_SECURITY_COLUMN_ACL" in forward
    assert '_sequence_acl(bind, _SECURITY_OWNER) != {"USAGE"}' in forward
    assert "_sequence_acl(bind, _API)" in forward
    assert "_table_acl(bind, _SECURITY_OWNER) or _table_acl(bind, _API)" in forward
    assert "_column_acl(bind, _API)" in forward
    assert "public_execute" in source
    assert "api_execute" in source
