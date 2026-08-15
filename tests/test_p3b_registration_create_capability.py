from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/f07d8e9f0a26_p3b_registration_create_capability.py"


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


def test_create_capability_is_expand_only_after_registration_dek_boundary() -> None:
    source = _source()
    assert 'revision = "f07d8e9f0a26"' in source
    assert 'down_revision = "e07d8e9f0a25"' in source
    assert "Legacy registration DML is intentionally not revoked here" in source


def test_create_capability_is_principal_and_admin_context_bound() -> None:
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
    assert source.count("\nSECURITY DEFINER\n") == 1
    assert source.count("SET search_path = pg_catalog") == 1
    assert source.count("SET row_security = on") == 1


def test_create_validates_canonical_metadata_and_envelope_header() -> None:
    source = _source()

    for token in (
        "p_id_type <> v_id_type",
        "pg_catalog.length(v_id_type) > 20",
        "p_country_code <> v_country_code",
        "pg_catalog.length(v_country_code) <> 2",
        "p_id_number_masked <> v_mask",
        "pg_catalog.length(v_mask) > 50",
        "p_entity_type <> v_entity_type",
        "pg_catalog.length(v_entity_type) <> 1",
        "pg_catalog.octet_length(p_payload_encrypted) < 32",
        "p_key_version < 1",
        "pg_catalog.get_byte(p_payload_encrypted, 0)::bigint * 16777216",
        "registration encrypted payload key version mismatch",
    ):
        assert token in source


def test_create_requires_active_key_for_same_tenant_and_registration_domain() -> None:
    source = _source()

    assert "FROM public.encryption_key_registry AS key_data" in source
    assert "key_data.tenant_id = v_org_id" in source
    assert "key_data.table_name = '{_KEY_SCOPE}'" in source
    assert "key_data.key_version = p_key_version" in source
    assert "key_data.key_status = 'ACTIVE'" in source
    assert "registration ACTIVE data key is required" in source
    assert "ERRCODE = '23503'" in source


def test_create_inserts_crypto_v1_metadata_and_secure_payload_in_one_function() -> None:
    source = _source()
    create_sql = source.split('_CREATE_FUNCTION = f"""', 1)[1].split(
        '"""\n\n\ndef _require_predecessor', 1
    )[0]

    metadata_insert = create_sql.index("INSERT INTO public.organization_registrations")
    payload_insert = create_sql.index(
        "INSERT INTO public.organization_registration_payloads_secure"
    )
    assert metadata_insert < payload_insert
    assert "id_number_encrypted" not in create_sql
    assert "crypto_version," in create_sql
    assert "1," in create_sql
    assert "FALSE," in create_sql
    assert "NULL" in create_sql
    assert "key_scope," in create_sql
    assert "schema_version" in create_sql
    assert "'{_KEY_SCOPE}'" in create_sql


def test_runtime_receives_function_execute_not_new_direct_secure_payload_dml() -> None:
    source = _source()
    upper = re.sub(r"\s+", " ", source).upper()

    assert (
        "GRANT EXECUTE ON FUNCTION APP_SECURE.CREATE_ORGANIZATION_REGISTRATION_ENVELOPE("
        "UUID,TEXT,TEXT,TEXT,TEXT,BYTEA,INTEGER) TO APP_RUNTIME"
    ) in upper
    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|TRUNCATE|ALL)\b[^;]*\b"
        r"ORGANIZATION_REGISTRATION_PAYLOADS_SECURE\b[^;]*\bTO\s+APP_RUNTIME\b",
        upper,
    )
    assert "GRANT ALL" not in upper
    assert "DISABLE ROW LEVEL SECURITY" not in upper
    assert "NO FORCE ROW LEVEL SECURITY" not in upper
    assert "ROW_SECURITY = OFF" not in upper
    assert not re.search(
        r"\b(?:CREATE|ALTER)\s+ROLE\s+[^;]*\bBYPASSRLS\b",
        upper,
        flags=re.DOTALL,
    )


def test_security_owner_insert_columns_are_exact_and_downgrade_reverses_them() -> None:
    source = _source()
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")
    forward = _function_source("_require_forward")

    for column in (
        "id",
        "org_id",
        "id_type",
        "id_number_masked",
        "country_code",
        "entity_type",
        "crypto_version",
        "is_verified",
        "verified_at",
    ):
        assert f'"{column}"' in source
    for column in (
        "registration_id",
        "tenant_id",
        "payload_encrypted",
        "key_version",
        "key_scope",
        "schema_version",
    ):
        assert f'"{column}"' in source

    assert "GRANT INSERT (" in upgrade
    assert "TO app_security_owner" in upgrade
    assert "REVOKE INSERT (" in downgrade
    assert "FROM app_security_owner" in downgrade
    assert "registration_insert != _REGISTRATION_INSERT_COLUMNS" in forward
    assert "payload_insert != _PAYLOAD_INSERT_COLUMNS" in forward

    for forbidden in (
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT TRUNCATE",
        "GRANT INSERT ON TABLE public.organization_registrations",
        "GRANT INSERT ON TABLE public.organization_registration_payloads_secure",
    ):
        assert forbidden not in upgrade


def test_function_installation_uses_temporary_schema_create_and_public_execute_is_revoked() -> None:
    install = _function_source("_install_function")

    assert "has_schema_privilege" in install
    assert "GRANT CREATE ON SCHEMA app_secure TO app_security_owner" in install
    assert "SET LOCAL ROLE app_security_owner" in install
    assert "REVOKE ALL ON FUNCTION" in install
    assert "FROM PUBLIC" in install
    assert "GRANT EXECUTE ON FUNCTION" in install
    assert "TO app_runtime" in install
    assert "RESET ROLE" in install
    assert "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner" in install
