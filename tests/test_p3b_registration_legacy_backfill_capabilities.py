from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/i07d8e9f0a29_p3b_registration_legacy_backfill_capabilities.py"


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


def test_backfill_revision_follows_replace_conflict_key_boundary() -> None:
    source = _source()
    assert 'revision = "i07d8e9f0a29"' in source
    assert 'down_revision = "h07d8e9f0a28"' in source


def test_backfill_read_exposes_legacy_ciphertext_only_for_bound_admin_context() -> None:
    source = _source()
    assert "current_legacy_registration_backfill_rows" in source
    assert "registration.id_number_encrypted::text" in source
    assert "registration.crypto_version = 0" in source
    assert "registration.org_id = v_org_id" in source
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
        assert token in source


def test_backfill_adds_only_two_temporary_registration_read_columns() -> None:
    source = _source()
    assert '_FORWARD_SELECT = _PREDECESSOR_SELECT | {"id_number_encrypted", "crypto_version"}' in source
    assert "GRANT SELECT (id_number_encrypted, crypto_version)" in source
    assert "TO app_security_owner" in source
    assert "REVOKE SELECT (id_number_encrypted, crypto_version)" in source
    assert "FROM app_security_owner" in source
    assert "GRANT SELECT ON TABLE public.organization_registrations" not in source


def test_conversion_preserves_verification_and_only_changes_crypto_material() -> None:
    source = _source()
    convert_sql = source.split('_CONVERT_SQL = f"""', 1)[1].split(
        '"""\n\n\ndef _install_functions', 1
    )[0]
    assert "id_number_encrypted = NULL" in convert_sql
    assert "crypto_version = 1" in convert_sql
    assert "updated_at = pg_catalog.clock_timestamp()" in convert_sql
    assert "registration.is_verified" in convert_sql
    assert "registration.verified_at" in convert_sql
    update_sql = convert_sql.split("UPDATE public.organization_registrations", 1)[1].split(
        "RETURNING", 1
    )[0]
    assert "is_verified =" not in update_sql
    assert "verified_at =" not in update_sql


def test_conversion_requires_active_same_tenant_registration_key_and_header_match() -> None:
    source = _source()
    assert "public.encryption_key_registry" in source
    assert "key_data.tenant_id = v_org_id" in source
    assert "key_data.table_name = '{_KEY_SCOPE}'" in source
    assert "key_data.key_status = 'ACTIVE'" in source
    assert "pg_catalog.get_byte(p_payload_encrypted, 0)" in source
    assert ") <> p_key_version::bigint" in source


def test_conversion_inserts_secure_payload_atomically_without_upsert() -> None:
    source = _source()
    convert_sql = source.split('_CONVERT_SQL = f"""', 1)[1].split(
        '"""\n\n\ndef _install_functions', 1
    )[0]
    assert convert_sql.index("UPDATE public.organization_registrations") < convert_sql.index(
        "INSERT INTO public.organization_registration_payloads_secure"
    )
    assert "ON CONFLICT" not in convert_sql
    assert "key_scope" in convert_sql
    assert "'{_KEY_SCOPE}'" in convert_sql


def test_backfill_capabilities_are_expand_window_only_and_not_public() -> None:
    install = _function_source("_install_functions")
    downgrade = _function_source("downgrade")
    assert "REVOKE ALL ON FUNCTION" in install
    assert "FROM PUBLIC" in install
    assert install.count("TO app_runtime") == 2
    assert "DROP FUNCTION app_secure.convert_legacy_organization_registration_envelope" in downgrade
    assert "DROP FUNCTION app_secure.current_legacy_registration_backfill_rows" in downgrade


def test_backfill_migration_never_weakens_rls_or_grants_broad_access() -> None:
    upper = _source().upper()
    for forbidden in (
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
        "GRANT ALL",
        "OWNER TO APP_RUNTIME",
        "OWNER TO APP_SECURITY_OWNER",
    ):
        assert forbidden not in upper
