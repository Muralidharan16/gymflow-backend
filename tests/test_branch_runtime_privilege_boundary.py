from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/708192a3b4c5_branch_runtime_privilege_boundary.py"
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


def test_branch_runtime_acl_is_operation_scoped() -> None:
    source = _source()
    grant = _function_source("_grant_contract")

    assert "GRANT SELECT ON TABLE public.org_branches TO app_runtime" in grant
    assert (
        "GRANT SELECT, UPDATE ON TABLE public.org_branch_state TO app_runtime"
        in grant
    )
    assert (
        "GRANT SELECT ON TABLE public.branch_geolocation_state TO app_runtime"
        in grant
    )
    assert (
        "GRANT INSERT, UPDATE ON TABLE public.org_branches TO auth_runtime"
        in grant
    )
    assert "GRANT INSERT ON TABLE public.org_branch_state TO auth_runtime" in grant

    auth_contract = source[
        source.index("_AUTH_BOOTSTRAP_PRIVILEGES"):
        source.index("_FORBIDDEN_PRIVILEGES")
    ]
    assert "_GEOLOCATION_STATE" not in auth_contract
    assert "_LIFECYCLE_REFERENCE_TABLES" not in auth_contract
    assert "_LIFECYCLE_APPEND_TABLES" not in auth_contract

    assert "GRANT ALL" not in source.upper()
    assert "ALTER ROLE" not in source
    assert "OWNER TO app_runtime" not in source
    assert "OWNER TO auth_runtime" not in source


def test_lifecycle_reference_catalogs_are_runtime_read_only() -> None:
    source = _source()
    grant = _function_source("_grant_contract")
    verify = _function_source("_verify_final_acl")

    for relation in (
        "public.branch_status_definitions",
        "public.branch_status_transitions",
        "public.branch_deactivation_policies",
    ):
        assert relation in source
    assert "for relation in _LIFECYCLE_REFERENCE_TABLES" in grant
    assert "GRANT SELECT ON TABLE {relation} TO app_runtime" in grant
    assert "_READ_ONLY_FORBIDDEN" in verify


def test_lifecycle_append_surfaces_are_select_insert_only() -> None:
    source = _source()
    grant = _function_source("_grant_contract")
    verify = _function_source("_verify_final_acl")

    for relation in (
        "public.branch_status_history",
        "public.branch_lifecycle_events",
        "public.branch_outbox_events",
        "public.branch_watchdog_alerts",
    ):
        assert relation in source
    assert "for relation in _LIFECYCLE_APPEND_TABLES" in grant
    assert "GRANT SELECT, INSERT ON TABLE {relation} TO app_runtime" in grant
    assert "_APPEND_FORBIDDEN" in verify
    assert '_APPEND_FORBIDDEN = _FORBIDDEN_PRIVILEGES | {"UPDATE"}' in source


def test_branch_runtime_acl_keeps_destructive_capabilities_forbidden() -> None:
    source = _source()

    assert (
        '_FORBIDDEN_PRIVILEGES = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}'
        in source
    )
    assert "must not have CREATE on public schema" in source
    assert "_require_no_public_dml(bind)" in source
    assert "acl.grantee = 0" in source
    assert "BYPASSRLS" in source


def test_branch_runtime_requires_reduced_roles_and_protected_base_relations() -> None:
    source = _source()

    assert "must retain ENABLE + FORCE ROW LEVEL SECURITY" in source
    assert "rolbypassrls" in source
    assert "NOLOGIN/NOINHERIT/NOBYPASSRLS" in source
    assert "branch runtime migration requires migration_owner" in source


def test_branch_geolocation_read_requires_existing_tenant_policy() -> None:
    source = _source()
    policy = _function_source("_require_geolocation_policy")

    assert '"geolocation_state_tenant_isolation"' in source
    assert "policy_data.polqual" in policy
    assert "policy_data.polwithcheck" in policy
    assert 'row["command"] != "*"' in policy
    assert "_TENANT_EXPR" in source
    assert "branch geolocation tenant policy drifted" in policy


def test_permissive_state_tenant_policy_is_removed_forward_and_exactly_restored() -> None:
    source = _source()
    preflight = _function_source("_require_predecessor_state_tenant_policy")
    drop_predecessor = _function_source("_drop_predecessor_lifecycle_policies")
    create_forward = _function_source("_create_forward_lifecycle_policies")
    create_predecessor = _function_source("_create_predecessor_lifecycle_policies")

    # 0006 created tenant_isolation_state as a permissive ALL policy for PUBLIC
    # with tenant-only USING. Keeping it beside later permissive role policies
    # would OR around those role restrictions.
    assert '"tenant_isolation_state"' in source
    assert "policy_data.polpermissive" in preflight
    assert "policy_data.polroles = ARRAY[0::oid]" in preflight
    assert 'row["command"] != "*"' in preflight
    assert "row[\"check_expr\"] is not None" in preflight
    assert "_TENANT_EXPR" in source

    assert (
        "DROP POLICY tenant_isolation_state ON public.org_branch_state"
        in drop_predecessor
    )
    assert "tenant_isolation_state" not in create_forward

    assert (
        "CREATE POLICY tenant_isolation_state ON public.org_branch_state"
        in create_predecessor
    )
    assert "app.current_org_id" in create_predecessor

    predecessor_inventory = source[
        source.index("_PREDECESSOR_POLICY_NAMES"):
        source.index("_FORWARD_POLICY_NAMES")
    ]
    forward_inventory = source[
        source.index("_FORWARD_POLICY_NAMES"):
        source.index("def _scalar")
    ]
    assert "tenant_isolation_state" in predecessor_inventory
    assert "tenant_isolation_state" not in forward_inventory


def test_lifecycle_child_policies_are_tenant_scoped_and_force_rls() -> None:
    source = _source()
    forward = _function_source("_create_forward_lifecycle_policies")
    verify = _function_source("_verify_forward_lifecycle_security")

    assert "tenant_branch.id = {short_name}.branch_id" in source
    assert "tenant_branch.org_id = {tenant}" in source
    assert "app.current_org_id" in source
    assert "p_history_insert" in source
    assert "p_events_insert" in source
    assert "p_outbox_insert" in source
    assert "p_watchdog_insert" in source
    assert "forward lifecycle RLS contract is not ENABLE+FORCE" in verify

    # The predecessor's system-role UPDATE escape must not survive forward.
    assert (
        "OR auth.role() IN ('system', 'saga_orchestrator', 'system_watchdog')"
        not in forward
    )


def test_lifecycle_admin_role_bridge_is_reversible() -> None:
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")

    assert "array_append(allowed_roles, 'admin')" in upgrade
    assert "'org_admin' = ANY(allowed_roles)" in upgrade
    assert "array_remove(allowed_roles, 'admin')" in downgrade
    assert "'org_admin' = ANY(allowed_roles)" in downgrade


def test_branch_runtime_downgrade_restores_predecessor_contract() -> None:
    downgrade = _function_source("downgrade")
    revoke = _function_source("_revoke_contract")

    assert "_verify_final_acl(bind)" in downgrade
    assert "_verify_forward_lifecycle_security(bind)" in downgrade
    assert "_revoke_contract()" in downgrade
    assert "_drop_forward_lifecycle_policies()" in downgrade
    assert "_create_predecessor_lifecycle_policies()" in downgrade
    assert "NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "_require_predecessor_lifecycle_security(bind)" in downgrade
    assert "_require_predecessor_acl(bind)" in downgrade

    assert "REVOKE SELECT ON TABLE public.org_branches FROM app_runtime" in revoke
    assert (
        "REVOKE SELECT, UPDATE ON TABLE public.org_branch_state FROM app_runtime"
        in revoke
    )
    assert (
        "REVOKE SELECT ON TABLE public.branch_geolocation_state FROM app_runtime"
        in revoke
    )
    assert "REVOKE SELECT ON TABLE {relation} FROM app_runtime" in revoke
    assert "REVOKE SELECT, INSERT ON TABLE {relation} FROM app_runtime" in revoke
    assert (
        "REVOKE INSERT, UPDATE ON TABLE public.org_branches FROM auth_runtime"
        in revoke
    )
    assert "REVOKE INSERT ON TABLE public.org_branch_state FROM auth_runtime" in revoke
