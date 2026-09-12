from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/g07d8e9f0a27_p3b_registration_replace_capability.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    return "".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def test_replace_revision_follows_atomic_create() -> None:
    source = _source()
    assert 'revision = "g07d8e9f0a27"' in source
    assert 'down_revision = "f07d8e9f0a26"' in source
    assert '_KEY_SCOPE = "organization_registrations"' in source


def test_replace_function_is_principal_and_tenant_bound() -> None:
    source = _source()
    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "public.owners",
        "public.organization_users",
        "ERRCODE = '42501'",
    ):
        assert token in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog" in source
    assert "SET row_security = on" in source
    assert "FROM PUBLIC" in source
    assert "TO app_runtime" in source


def test_replace_validates_active_same_domain_key_and_envelope_header() -> None:
    source = _source()
    assert "public.encryption_key_registry" in source
    assert "key_data.tenant_id = v_org_id" in source
    assert "key_data.table_name = '{_KEY_SCOPE}'" in source
    assert "key_data.key_status = 'ACTIVE'" in source
    assert "pg_catalog.get_byte(p_payload_encrypted, 0)" in source
    assert ") <> p_key_version::bigint" in source


def test_identifier_change_invalidates_previous_verification() -> None:
    source = _source()
    replace_sql = source.split('_REPLACE_FUNCTION = f"""', 1)[1]
    assert "id_number_encrypted = NULL" in replace_sql
    assert "crypto_version = 1" in replace_sql
    assert "is_verified = FALSE" in replace_sql
    assert "verified_at = NULL" in replace_sql
    assert "updated_at = pg_catalog.clock_timestamp()" in replace_sql


def test_legacy_and_existing_envelope_rows_share_one_atomic_replace_path() -> None:
    source = _source()
    replace_sql = source.split('_REPLACE_FUNCTION = f"""', 1)[1]
    assert "UPDATE public.organization_registrations" in replace_sql
    assert "INSERT INTO public.organization_registration_payloads_secure" in replace_sql
    assert "ON CONFLICT (registration_id) DO UPDATE" in replace_sql
    assert "payload_encrypted = EXCLUDED.payload_encrypted" in replace_sql
    assert "key_version = EXCLUDED.key_version" in replace_sql


def test_security_owner_receives_exact_update_columns_only() -> None:
    source = _source()
    assert '_REGISTRATION_UPDATE_COLUMNS = {' in source
    for column in (
        "id_number_encrypted",
        "id_number_masked",
        "entity_type",
        "crypto_version",
        "is_verified",
        "verified_at",
        "updated_at",
    ):
        assert f'"{column}"' in source
    assert '_PAYLOAD_UPDATE_COLUMNS = {' in source
    for column in (
        "payload_encrypted",
        "key_version",
        "key_scope",
        "schema_version",
        "updated_at",
    ):
        assert f'"{column}"' in source
    assert "GRANT UPDATE (" in source
    assert "REVOKE UPDATE (" in source
    assert "GRANT UPDATE ON TABLE" not in source
    assert "GRANT ALL" not in source


def test_forward_attestation_pins_no_public_execution_and_exact_acl() -> None:
    forward = _function_source("_require_forward")
    assert "registration_updates != _REGISTRATION_UPDATE_COLUMNS" in forward
    assert "payload_updates != _PAYLOAD_UPDATE_COLUMNS" in forward
    assert 'row["owner_name"] != _SECURITY_OWNER' in forward
    assert 'not bool(row["prosecdef"])' in forward
    assert 'bool(row["public_execute"])' in forward
    assert 'not bool(row["api_execute"])' in forward
    assert '"is_verified = false"' in forward
    assert '"verified_at = null"' in forward
