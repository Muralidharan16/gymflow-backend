from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/8192a3b4c5d6_lifecycle_runtime_security_boundary.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    module = ast.parse(_source(), filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.unparse(node)


def test_lifecycle_reference_catalogs_are_runtime_read_only() -> None:
    source = _source()
    grant = _function_source("_grant_forward_acl")

    for relation in (
        "public.branch_status_definitions",
        "public.branch_status_transitions",
        "public.branch_deactivation_policies",
    ):
        assert f"GRANT SELECT ON TABLE {relation} TO app_runtime" in source or "_REFERENCE_TABLES" in grant

    assert '_FORBIDDEN_RUNTIME = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}' in source
    assert "GRANT ALL" not in source.upper()
    assert "ALTER ROLE" not in source
    assert "BYPASSRLS" not in grant.upper()


def test_lifecycle_append_surfaces_never_receive_runtime_update_or_delete() -> None:
    source = _source()
    verify = _function_source("_verify_forward")

    assert 'observed != {"SELECT", "INSERT"}' in verify
    assert '_FORBIDDEN_RUNTIME | {"UPDATE"}' in verify
    for relation in (
        "public.branch_status_history",
        "public.branch_lifecycle_events",
        "public.branch_outbox_events",
        "public.branch_watchdog_alerts",
    ):
        assert relation in source


def test_lifecycle_tenant_relations_are_force_rls_and_branch_scoped() -> None:
    source = _source()
    forward = _function_source("_create_forward_policies")

    assert "FORCE ROW LEVEL SECURITY" in source
    assert "tenant_branch.id = {short_name}.branch_id" in source
    assert "tenant_branch.org_id = {tenant}" in source
    assert "app.current_org_id" in source
    assert "p_history_insert" in source
    assert "p_events_insert" in source
    assert "p_outbox_insert" in source
    assert "p_watchdog_insert" in source

    # The predecessor's role-only/cross-tenant write escape must not survive in
    # the forward branch-state policies.
    assert "OR auth.role() IN ('system', 'saga_orchestrator', 'system_watchdog')" not in forward
    assert "org_id = {tenant}" in source


def test_lifecycle_admin_compatibility_bridge_is_reversible() -> None:
    source = _source()
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "array_append(allowed_roles, 'admin')" in upgrade
    assert "'org_admin' = ANY(allowed_roles)" in upgrade
    assert "array_remove(allowed_roles, 'admin')" in downgrade
    assert "'org_admin' = ANY(allowed_roles)" in downgrade


def test_lifecycle_downgrade_restores_predecessor_acl_policies_and_force_state() -> None:
    source = _source()
    downgrade = _function_source("downgrade")

    assert "REVOKE SELECT ON TABLE {relation} FROM app_runtime" in source
    assert "REVOKE SELECT, INSERT ON TABLE {relation} FROM app_runtime" in source
    assert "_drop_forward_policies()" in downgrade
    assert "_create_predecessor_policies()" in downgrade
    assert "NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "_require_predecessor_security(bind)" in downgrade
