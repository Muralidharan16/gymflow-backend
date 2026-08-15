from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/j07d8e9f0a2a_p3b_registration_replace_without_upsert_reads.py"


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


def test_replace_correction_revision_follows_backfill_window() -> None:
    source = _source()
    assert 'revision = "j07d8e9f0a2a"' in source
    assert 'down_revision = "i07d8e9f0a29"' in source
    assert "No ACL changes occur" in source


def test_corrected_payload_path_uses_insert_then_unique_violation_update() -> None:
    source = _source()
    corrected = source.split('_CORRECTED_PAYLOAD_SQL = f"""', 1)[1].split(
        '"""\n\n\ndef _require_function_contract', 1
    )[0]
    assert "INSERT INTO public.organization_registration_payloads_secure" in corrected
    assert "WHEN unique_violation THEN" in corrected
    assert "UPDATE public.organization_registration_payloads_secure AS payload" in corrected
    assert "payload.registration_id = p_registration_id" in corrected
    assert "payload.tenant_id = v_org_id" in corrected
    assert "IF NOT FOUND THEN" in corrected
    assert "RAISE;" in corrected
    assert "ON CONFLICT" not in corrected


def test_correction_does_not_grant_or_revoke_payload_or_registration_acl() -> None:
    source = _source()
    for forbidden in (
        "GRANT SELECT (registration_id",
        "GRANT SELECT (payload_encrypted",
        "GRANT UPDATE (payload_encrypted",
        "GRANT INSERT (registration_id",
        "REVOKE SELECT (registration_id",
        "REVOKE UPDATE (payload_encrypted",
        "REVOKE INSERT (registration_id",
        "GRANT ALL",
    ):
        assert forbidden not in source


def test_replace_function_preserves_principal_key_and_verification_contracts() -> None:
    source = _source()
    function_builder = _function_source("_replace_function")
    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "public.owners",
        "public.organization_users",
        "public.encryption_key_registry",
        "key_data.key_status = 'ACTIVE'",
        "id_number_encrypted = NULL",
        "crypto_version = 1",
        "is_verified = FALSE",
        "verified_at = NULL",
        "SECURITY DEFINER",
        "SET search_path = pg_catalog",
        "SET row_security = on",
    ):
        assert token in function_builder


def test_upgrade_and_downgrade_swap_only_function_implementation() -> None:
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")
    assert "_require_function_contract(bind, corrected=False)" in upgrade
    assert "_replace_as_security_owner(bind, _replace_function(_CORRECTED_PAYLOAD_SQL))" in upgrade
    assert "_require_function_contract(bind, corrected=True)" in upgrade
    assert "_require_function_contract(bind, corrected=True)" in downgrade
    assert "_replace_as_security_owner(bind, _replace_function(_PREDECESSOR_PAYLOAD_SQL))" in downgrade
    assert "_require_function_contract(bind, corrected=False)" in downgrade


def test_function_replacement_uses_temporary_schema_create_and_preserves_execute_acl() -> None:
    install = _function_source("_replace_as_security_owner")
    assert "GRANT CREATE ON SCHEMA app_secure TO app_security_owner" in install
    assert "SET LOCAL ROLE app_security_owner" in install
    assert "REVOKE ALL ON FUNCTION" in install
    assert "FROM PUBLIC" in install
    assert "GRANT EXECUTE ON FUNCTION" in install
    assert "TO app_runtime" in install
    assert "RESET ROLE" in install
    assert "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner" in install


def test_migration_never_weakens_rls_or_role_identity() -> None:
    upper = _source().upper()
    for forbidden in (
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
        "OWNER TO APP_RUNTIME",
        "OWNER TO APP_SECURITY_OWNER",
    ):
        assert forbidden not in upper
