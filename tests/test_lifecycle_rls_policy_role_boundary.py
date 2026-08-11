from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "718293a4b5c6_scope_lifecycle_rls_policies_by_role.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_lifecycle_role_scoping_alters_only_policy_audience() -> None:
    source = _source()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_alter_policy_roles"
    )
    body = ast.get_source_segment(source, function) or ""

    assert "ALTER POLICY {policy_name} ON {relation} TO {role_name}" in body
    assert "CREATE POLICY" not in body
    assert "DROP POLICY" not in body
    assert "USING (" not in body
    assert "WITH CHECK" not in body


def test_policy_contract_verification_preserves_predicates() -> None:
    source = _source()
    assert "_predicate_contract(row)" in source
    assert "changed lifecycle policy predicate while scoping roles" in source
    assert "downgrade changed lifecycle policy predicate" in source
    assert '"p_branch_insert": "auth_runtime"' in source
    assert '"p_branch_select": "app_runtime"' in source
    assert '"p_outbox_select": "app_runtime"' in source


def test_worker_and_internal_policies_remain_separate() -> None:
    source = _source()
    assert "lifecycle_worker_branch_read" in source
    assert "lifecycle_worker_outbox_select" in source
    assert "lifecycle_internal_outbox_read" in source
    assert "branch_hours_worker_branch_read" in source
    assert "branch_hours_internal_enqueue_branch_read" in source


def test_policy_scoping_does_not_grant_worker_or_security_owner_capability() -> None:
    normalized = " ".join(_source().upper().split())
    forbidden = (
        "GRANT SELECT ON TABLE PUBLIC.ORGANIZATIONS TO WORKER_RUNTIME",
        "GRANT UPDATE ON TABLE PUBLIC.ORGANIZATIONS TO WORKER_RUNTIME",
        "GRANT INSERT ON TABLE PUBLIC.BRANCH_OUTBOX_EVENTS TO WORKER_RUNTIME",
        "GRANT CREATE ON SCHEMA PUBLIC TO WORKER_RUNTIME",
        "GRANT CREATE ON SCHEMA PUBLIC TO APP_SECURITY_OWNER",
        "ALTER ROLE WORKER_RUNTIME BYPASSRLS",
        "ALTER ROLE APP_SECURITY_OWNER BYPASSRLS",
    )
    for statement in forbidden:
        assert statement not in normalized
