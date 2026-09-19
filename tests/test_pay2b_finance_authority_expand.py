from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zl07d8e9f0a46_pay2_finance_authority_expand.py"
SOURCE = MIGRATION.read_text(encoding="utf-8")

FINANCE_ROLES = {
    "finance_runtime",
    "finance_read_runtime",
    "payment_worker_runtime",
    "refund_runtime",
    "finance_reconciliation_runtime",
    "finance_maintenance_runtime",
}


def test_pay2b_migration_is_single_linear_successor():
    assert 'revision = "zl07d8e9f0a46"' in SOURCE
    assert 'down_revision = "zk07d8e9f0a45"' in SOURCE
    ast.parse(SOURCE)


def test_pay2b_never_manages_cluster_roles():
    upper = SOURCE.upper()
    assert "CREATE ROLE" not in upper
    assert "ALTER ROLE" not in upper
    assert "DROP ROLE" not in upper
    assert "GRANT APP_SECURITY_OWNER TO" not in upper
    assert "GRANT APP_RLS_EXECUTOR TO" not in upper


def test_pay2b_requires_all_dedicated_finance_roles_and_no_migration_reachability():
    for role in FINANCE_ROLES:
        assert f'"{role}"' in SOURCE
    for semantic in ("MEMBER", "USAGE", "SET"):
        assert f'"{semantic}"' in SOURCE
    assert "PAY-2B migration_owner leaked into Finance runtime role" in SOURCE


def test_pay2b_refuses_to_repair_preexisting_finance_authority():
    assert "PAY-2B refuses pre-existing Finance schema authority" in SOURCE
    assert "PAY-2B refuses pre-existing direct Finance table authority" in SOURCE
    assert "PAY-2B refuses pre-existing app_secure function authority" in SOURCE
    assert "information_schema.role_table_grants" in SOURCE
    assert "information_schema.routine_privileges" in SOURCE


def test_pay2b_guard_is_bounded_security_definer():
    assert "CREATE FUNCTION app_secure.require_finance_capability(" in SOURCE
    assert "SECURITY DEFINER" in SOURCE
    assert "SET search_path=pg_catalog,public,finance" in SOURCE
    assert "SET row_security=on" in SOURCE
    assert "REVOKE ALL ON FUNCTION " in SOURCE
    assert "FROM PUBLIC" in SOURCE
    assert "EXECUTE IMMEDIATE" not in SOURCE.upper()
    assert "FORMAT(" not in SOURCE.upper()


def test_pay2b_guard_maps_exact_capabilities_to_exact_roles():
    mapping = {
        "command": "finance_runtime",
        "read": "finance_read_runtime",
        "payment_worker": "payment_worker_runtime",
        "refund": "refund_runtime",
        "reconciliation": "finance_reconciliation_runtime",
        "maintenance": "finance_maintenance_runtime",
    }
    for capability, role in mapping.items():
        assert f"WHEN '{capability}' THEN" in SOURCE
        assert f"v_required_role := '{role}'" in SOURCE


def test_pay2b_tenant_bypass_is_limited_to_reconciliation_and_maintenance():
    assert "p_capability NOT IN ('reconciliation','maintenance')" in SOURCE
    assert "PAY2_FINANCE_TENANT_CONTEXT_REQUIRED" in SOURCE
    assert "current_setting('app.current_org_id', true)" in SOURCE


def test_pay2b_dedicated_roles_gain_no_direct_finance_table_or_schema_access():
    assert "GRANT USAGE ON SCHEMA finance" not in SOURCE
    assert "GRANT SELECT ON TABLE finance." not in SOURCE
    assert "GRANT INSERT ON TABLE finance." not in SOURCE
    assert "GRANT UPDATE ON TABLE finance." not in SOURCE
    assert "GRANT DELETE ON TABLE finance." not in SOURCE
    assert "has_schema_privilege(:role,'finance','USAGE')" in SOURCE
    assert "role_table_grants" in SOURCE


def test_pay2b_downgrade_removes_only_pay2b_database_capability():
    downgrade = SOURCE.split("def downgrade()", 1)[1]
    assert "DROP FUNCTION app_secure.require_finance_capability(text,boolean)" in downgrade
    assert "for role_name in _FINANCE_ROLES:" in downgrade
    assert 'op.execute(f"REVOKE USAGE ON SCHEMA app_secure FROM {role_name}")' in downgrade
    for role in FINANCE_ROLES:
        assert f'"{role}"' in SOURCE
    assert "DROP ROLE" not in downgrade.upper()
    assert "DELETE FROM finance." not in downgrade
    assert "TRUNCATE" not in downgrade.upper()
