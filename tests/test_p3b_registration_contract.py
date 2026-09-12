from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/k07d8e9f0a2b_p3b_registration_contract.py"


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


def test_contract_revision_follows_corrected_replace_lineage() -> None:
    source = _source()
    assert 'revision = "k07d8e9f0a2b"' in source
    assert 'down_revision = "j07d8e9f0a2a"' in source


def test_upgrade_hard_stops_on_legacy_or_missing_envelope_rows() -> None:
    upgrade = _function_source("upgrade")
    assert "ADD CONSTRAINT ck_org_reg_envelope_only" in upgrade
    assert "CHECK (crypto_version = 1 AND id_number_encrypted IS NULL)" in upgrade
    assert "NOT VALID" in upgrade
    assert "VALIDATE CONSTRAINT ck_org_reg_envelope_only" in upgrade
    assert "ADD CONSTRAINT fk_org_reg_required_envelope" in upgrade
    assert "FOREIGN KEY (id)" in upgrade
    assert "REFERENCES public.organization_registration_payloads_secure (registration_id)" in upgrade
    assert "DEFERRABLE INITIALLY DEFERRED" in upgrade
    assert "VALIDATE CONSTRAINT fk_org_reg_required_envelope" in upgrade


def test_contract_closes_only_temporary_backfill_read_and_execute_surface() -> None:
    upgrade = _function_source("upgrade")
    assert "ALTER COLUMN crypto_version SET DEFAULT 1" in upgrade
    assert "_drop_backfill_functions(bind)" in upgrade
    assert "REVOKE SELECT (id_number_encrypted, crypto_version)" in upgrade
    assert "FROM app_security_owner" in upgrade
    assert "REVOKE ALL ON TABLE" not in upgrade
    assert "REVOKE ALL PRIVILEGES" not in upgrade


def test_downgrade_restores_exact_migration_window_capability() -> None:
    downgrade = _function_source("downgrade")
    assert "GRANT SELECT (id_number_encrypted, crypto_version)" in downgrade
    assert "TO app_security_owner" in downgrade
    assert "_install_backfill_functions(bind)" in downgrade
    assert "ALTER COLUMN crypto_version SET DEFAULT 0" in downgrade
    assert "DROP CONSTRAINT fk_org_reg_required_envelope" in downgrade
    assert "DROP CONSTRAINT ck_org_reg_envelope_only" in downgrade
    assert "_require_predecessor(bind)" in downgrade


def test_backfill_recreation_remains_principal_bound_and_non_public() -> None:
    source = _source()
    guard = _function_source("_principal_guard_sql")
    install = _function_source("_install_backfill_functions")
    for token in (
        "app.current_org_id",
        "app.current_user_id",
        "app.current_principal_type",
        "app.current_role",
        "app.current_gym_id",
        "public.owners",
        "public.organization_users",
    ):
        assert token in guard
    for token in (
        "REVOKE ALL ON FUNCTION",
        "FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION",
        "TO app_runtime",
        "SET LOCAL ROLE app_security_owner",
        "REVOKE CREATE ON SCHEMA app_secure FROM app_security_owner",
    ):
        assert token in install
    assert "id_number_encrypted" in source
    assert "crypto_version = 0" in source
    assert "crypto_version = 1" in source


def test_contract_attests_exact_final_select_surface() -> None:
    source = _source()
    assert '"id_number_encrypted", "crypto_version"' in source
    assert '"id_number_masked"' in source
    assert '_PAYLOAD_SELECT = {"registration_id", "tenant_id"}' in source
    final = _function_source("_require_final")
    assert "_FINAL_SELECT" in final
    assert "_PAYLOAD_SELECT" not in final  # payload SELECT is pinned in shared base boundary
    base = _function_source("_require_base_boundary")
    assert "_PAYLOAD_SELECT" in base
    assert "app_user" in base
    assert "auth_runtime" in base


def test_contract_never_weakens_rls_or_runtime_identity() -> None:
    upper = _source().upper()
    for forbidden in (
        "DISABLE ROW LEVEL SECURITY",
        "NO FORCE ROW LEVEL SECURITY",
        "ROW_SECURITY = OFF",
        "OWNER TO APP_RUNTIME",
        "OWNER TO APP_SECURITY_OWNER",
        "GRANT BYPASSRLS",
        "ALTER ROLE APP_RUNTIME BYPASSRLS",
        "GRANT ALL ON TABLE",
        "GRANT ALL PRIVILEGES",
    ):
        assert forbidden not in upper
