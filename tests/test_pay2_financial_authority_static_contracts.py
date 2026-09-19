from __future__ import annotations

import json
from pathlib import Path

from app.core.cluster_identity_graph import validate_identity_transition_policy
from app.core.cluster_role_bootstrap import render_fresh_cluster_bootstrap
from app.core.cluster_role_contract import load_contract_bundle
from app.core.pay2_finance_roles import (
    PAY2_FINANCE_CAPABILITY_ROLES,
    expansion_contract_bundle,
    predecessor_contract_bundle,
    predecessor_identity_policy,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/zl07d8e9f0a46_pay2_financial_authority_database_security.py"
CONTRACT = ROOT / "docs/architecture/pay2_financial_authority_v1.json"
RELEASE = ROOT / "scripts/release/expand_pay2_finance_roles.sh"


def test_pay2_role_projection_and_predecessor_are_exact():
    full = load_contract_bundle()
    predecessor = predecessor_contract_bundle(full)
    expansion = expansion_contract_bundle(full)
    roles = set(PAY2_FINANCE_CAPABILITY_ROLES)
    assert roles <= set(full.roles["managed_roles"])
    assert roles.isdisjoint(predecessor.roles["managed_roles"])
    assert set(expansion.roles["managed_roles"]) == roles
    assert expansion.memberships["exact_rows"] == []
    assert validate_identity_transition_policy(
        predecessor, predecessor_identity_policy()
    ) == ()


def test_pay2_expansion_sql_is_additive_and_membership_free():
    sql = render_fresh_cluster_bootstrap(expansion_contract_bundle())
    for role in PAY2_FINANCE_CAPABILITY_ROLES:
        assert sql.count(f"CREATE ROLE {role} ") == 1
        assert f"ALTER ROLE {role} SET row_security = 'on';" in sql
    assert "GRANT " not in sql
    assert "DROP ROLE" not in sql


def test_pay2_migration_never_mutates_cluster_roles():
    source = MIGRATION.read_text(encoding="utf-8")
    upper = source.upper()
    assert 'REVISION = "ZL07D8E9F0A46"' in upper
    assert 'DOWN_REVISION = "ZK07D8E9F0A45"' in upper
    for token in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "GRANT FINANCE_RUNTIME TO"):
        assert token not in upper
    for role in PAY2_FINANCE_CAPABILITY_ROLES:
        assert role in source


def test_pay2_immutable_finance_evidence_is_database_enforced():
    source = MIGRATION.read_text(encoding="utf-8")
    namespace: dict[str, object] = {}
    module = compile(source, str(MIGRATION), "exec", dont_inherit=True, optimize=0)
    # Do not execute migration code; recover the tuple from the parsed assignment.
    import ast
    tree = ast.parse(source, filename=str(MIGRATION))
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_IMMUTABLE_TABLES" for target in node.targets)
    )
    immutable_tables = ast.literal_eval(assignment.value)
    assert set(immutable_tables) == {
        "audit_events","payment_events","ledger_entry_lines","tax_records","credit_note_lines"
    }
    assert "BEFORE UPDATE OR DELETE ON finance.{table_name}" in source
    assert "pay2_reject_finance_immutable_history_mutation" in source
    assert "pay2_guard_posted_finance_ledger_entry_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "OLD.status = 'posted'" in source
    assert "session_user = 'migration_owner'" in source
    assert "FROM PUBLIC" in source
    assert "SECURITY DEFINER" in source
    assert "SET row_security=on" in source
    assert "GRANT TRIGGER ON TABLE finance." in source
    assert "REVOKE TRIGGER ON TABLE finance." in source
    assert "PAY-2 installation-only TRIGGER privilege leaked" in source
    assert "SET LOCAL lock_timeout='3s'" in source
    assert "SET LOCAL statement_timeout='30s'" in source


def test_pay2_runtime_roles_receive_no_direct_finance_grants():
    source = MIGRATION.read_text(encoding="utf-8")
    for role in PAY2_FINANCE_CAPABILITY_ROLES:
        assert f"GRANT SELECT ON" not in source
        assert f"GRANT INSERT ON" not in source
        assert f"GRANT UPDATE ON" not in source
        assert f"GRANT DELETE ON" not in source
    assert "_has_direct_finance_relation_authority" in source
    assert "has_any_column_privilege" in source
    assert "_has_finance_object_ownership" in source


def test_pay2_release_expansion_is_outside_alembic_and_fail_closed():
    source = RELEASE.read_text(encoding="utf-8")
    assert "verify_pay2_finance_role_state.py predecessor" in source
    assert "render_pay2_finance_role_expansion.py" in source
    assert "verify_pay2_finance_role_state.py full" in source
    assert "alembic" not in source.lower()


def test_pay2_machine_contract_keeps_money_disabled():
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["phase"] == "PAY-2"
    assert data["alembic"] == {
        "predecessor": "zk07d8e9f0a45",
        "head": "zl07d8e9f0a46",
    }
    assert data["rules"]["live_money_movement"] is False
    assert data["rules"]["refund_provider_execution"] == "DEFERRED_FAIL_CLOSED"
    assert data["migration_safety"]["table_rewrite"] is False
    assert data["migration_safety"]["data_backfill"] is False


def test_pay2_migration_is_metadata_only_and_rewrite_free_by_construction():
    source = MIGRATION.read_text(encoding="utf-8").upper()
    for forbidden in (
        "ADD COLUMN",
        "ALTER COLUMN",
        "SET DATA TYPE",
        "UPDATE FINANCE.",
        "INSERT INTO FINANCE.",
        "DELETE FROM FINANCE.",
        "CREATE TABLE FINANCE.",
        "DROP TABLE FINANCE.",
    ):
        assert forbidden not in source
