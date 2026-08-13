from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "60718293a4b5_break_branch_hours_worker_rls_recursion.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_worker_source_policy_replacement_uses_security_owned_predicates() -> None:
    source = _source()

    assert "public.branch_hours_worker_has_live_lease(org_id, id)" in source
    assert "public.branch_hours_worker_has_live_lease(org_id, branch_id)" in source
    assert "public.branch_hours_worker_can_access_branch" in source

    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_create_nonrecursive_source_policies"
    )
    policy_source = ast.get_source_segment(source, function) or ""

    # Cross-FORCE-RLS traversal under worker_runtime was the recursion source.
    # Source policies may call only the bounded predicates; they must not embed
    # queue or branch subqueries directly again.
    assert "FROM public.transactional_outbox" not in policy_source
    assert "FROM public.org_branches AS branch_data" not in policy_source


def test_lease_predicates_are_security_definer_and_not_public() -> None:
    source = _source()
    assert source.count("SECURITY DEFINER") >= 2
    assert source.count("SET search_path = pg_catalog, public") >= 2
    assert source.count("SET row_security = on") >= 2
    assert "REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime" in source
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in source


def test_recursion_fix_does_not_expand_worker_tenant_root_or_queue_insert() -> None:
    source = _source()
    normalized = " ".join(source.upper().split())

    assert "GRANT INSERT ON TABLE PUBLIC.TRANSACTIONAL_OUTBOX TO WORKER_RUNTIME" not in normalized
    assert "GRANT SELECT ON TABLE PUBLIC.ORGANIZATIONS TO WORKER_RUNTIME" not in normalized
    assert "GRANT UPDATE ON TABLE PUBLIC.ORGANIZATIONS TO WORKER_RUNTIME" not in normalized
    assert "ALTER ROLE WORKER_RUNTIME BYPASSRLS" not in normalized
    assert "ALTER ROLE WORKER_RUNTIME WITH BYPASSRLS" not in normalized
