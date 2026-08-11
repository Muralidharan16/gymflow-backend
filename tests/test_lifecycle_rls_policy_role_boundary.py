from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "718293a4b5c6_scope_lifecycle_rls_policies_by_role.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_normal_lifecycle_policies_are_not_public() -> None:
    source = _source()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_scoped_policies"
    )
    body = ast.get_source_segment(source, function) or ""

    assert "FOR SELECT TO app_runtime" in body
    assert "FOR UPDATE TO app_runtime" in body
    assert "FOR INSERT TO auth_runtime" in body
    assert "FOR INSERT TO app_runtime" in body

    # The scoped forward policy creator must never silently recreate a policy
    # without an explicit database role domain.
    for marker in ("FOR SELECT\n", "FOR INSERT\n", "FOR UPDATE\n", "FOR DELETE\n"):
        assert marker not in body


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
