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
    assert "GRANT EXECUTE ON FUNCTION APP_SECURE.INSTALL_REGISTRATION_DEK(BYTEA,TEXT) TO APP_RUNTIME" in upper

    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|TRUNCATE|ALL)\b[^;]*\b"
        r"ENCRYPTION_KEY_REGISTRY\b[^;]*\bTO\s+APP_RUNTIME\b",
        upper,
    )
    assert not re.search(
        r"GRANT\s+(?:USAGE|SELECT|UPDATE|ALL)\b[^;]*\b"
        r"ENCRYPTION_KEY_REGISTRY_KEY_VERSION_SEQ\b[^;]*\bTO\s+APP_RUNTIME\b",
        upper,
    )
    assert "GRANT ALL" not in upper
    assert "OWNER TO APP_RUNTIME" not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_wrapping_key_identity_is_persisted_and_required_for_registration_keys() -> None:
    source = _source()
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "ADD COLUMN wrapping_key_id varchar(2048)" in upgrade
    assert "ck_key_registry_registration_wrapping_key" in upgrade
    assert "table_name <> 'organization_registrations'" in upgrade
    assert "wrapping_key_id IS NOT NULL" in upgrade
    assert "pg_catalog.btrim(wrapping_key_id) <> ''" in upgrade
    assert "P3B DEK downgrade would discard registration wrapping-key identity" in downgrade
    assert "DROP COLUMN wrapping_key_id" in downgrade


def test_security_owner_key_acl_is_narrow_and_reversible() -> None:
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "GRANT SELECT (table_name, key_status, wrapping_key_id)," in upgrade
    assert "tenant_id," in upgrade
    assert "encrypted_dek," in upgrade
    assert "wrapping_key_id" in upgrade
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
    assert "REVOKE SELECT (table_name, key_status, wrapping_key_id)," in downgrade
    assert "FROM app_security_owner" in downgrade
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

    assert source.count("table_name = '{_KEY_SCOPE}'") >= 4
    assert source.count("\nSECURITY DEFINER\n") == 3
    assert source.count("SET search_path = pg_catalog") == 3
    assert source.count("SET row_security = on") == 3
    assert source.count("wrapping_key_id") >= 12


def test_first_key_installation_is_database_serialized_and_race_safe() -> None:
    source = _source()
    assert "pg_catalog.pg_advisory_xact_lock" in source
    assert "pg_catalog.hashtextextended" in source
    assert "v_org_id::text || '|{_KEY_SCOPE}'" in source
    assert "AND key_data.key_status = 'ACTIVE'" in source
    assert "WHEN unique_violation THEN" in source
    assert "IF NOT FOUND THEN" in source
    assert "RAISE;" in source


def test_installer_validates_ciphertext_and_wrapping_key_before_insert() -> None:
    source = _source()
    install_sql = source.split('_INSTALL_FUNCTION = f"""', 1)[1].split('"""\n\n\ndef _require_predecessor', 1)[0]

    ciphertext_check = "IF p_encrypted_dek IS NULL OR pg_catalog.octet_length(p_encrypted_dek) = 0"
    wrapping_check = "IF p_wrapping_key_id IS NULL"
    insert_at = install_sql.index("INSERT INTO public.encryption_key_registry")
    assert ciphertext_check in install_sql
    assert wrapping_check in install_sql
    assert "pg_catalog.length(p_wrapping_key_id) > 2048" in install_sql
    assert install_sql.index(ciphertext_check) < insert_at
    assert install_sql.index(wrapping_check) < insert_at
    assert "pg_catalog.btrim(p_wrapping_key_id)" in install_sql
    assert "ERRCODE = '22023'" in install_sql


def test_historical_lookup_returns_wrapped_key_and_exact_wrapping_key_identity() -> None:
    source = _source()
    lookup_sql = source.split('_LOOKUP_FUNCTION = f"""', 1)[1].split('"""\n\n_INSTALL_FUNCTION', 1)[0]

    assert "RETURNS TABLE (encrypted_dek bytea, wrapping_key_id text)" in lookup_sql
    assert "p_key_version IS NULL OR p_key_version < 1" in lookup_sql
    assert "key_data.tenant_id = v_org_id" in lookup_sql
    assert "key_data.table_name = '{_KEY_SCOPE}'" in lookup_sql
    assert "key_data.key_version = p_key_version" in lookup_sql
    assert "key_data.wrapping_key_id::text" in lookup_sql


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


def test_forward_attestation_pins_exact_column_sequence_and_function_contracts() -> None:
    source = _source()
    forward = _function_source("_require_forward")

    assert "_FORWARD_SECURITY_COLUMN_ACL" in forward
    assert '("wrapping_key_id", "SELECT")' in source
    assert '("wrapping_key_id", "INSERT")' in source
    assert '_sequence_acl(bind, _SECURITY_OWNER) != {"USAGE"}' in forward
    assert "_sequence_acl(bind, _API)" in forward
    assert "_table_acl(bind, _SECURITY_OWNER) or _table_acl(bind, _API)" in forward
    assert "_column_acl(bind, _API)" in forward
    assert '"install_registration_dek": 2' in source
    assert "public_execute" in source
    assert "api_execute" in source
