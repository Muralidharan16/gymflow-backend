from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/8192a3b4c5d6_harden_legacy_branch_trigger_runtime_boundary.py"
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


def test_revision_extends_reduced_branch_runtime_head() -> None:
    source = _source()

    assert 'revision = "8192a3b4c5d6"' in source
    assert 'down_revision = "708192a3b4c5"' in source
    assert "GRANT ALL" not in source.upper()
    assert "BYPASSRLS" in source


def test_legacy_rbac_lookup_is_privileged_but_runtime_remains_blind() -> None:
    source = _source()
    forward = _function_source("_create_forward_rbac_guard")
    verify = _function_source("_verify_forward")

    assert "SECURITY DEFINER" in forward
    assert "SET search_path = pg_catalog, public, pg_temp" in forward
    assert "FROM public.gym_owners AS owner_data" in forward
    assert "NULLIF(" in forward

    # The no-login security owner receives only the three columns required for
    # the bounded role lookup.  Ordinary runtime must never receive direct
    # access to the legacy staff table.
    assert "GRANT SELECT (id, org_id, role)" in forward
    assert "TO app_security_owner" in forward
    assert "GRANT SELECT ON TABLE public.gym_owners TO app_runtime" not in source
    assert "GRANT SELECT (id, org_id, role) ON TABLE public.gym_owners TO app_runtime" not in source
    assert "runtime_table_select" in verify
    assert "runtime_id_select" in verify
    assert "runtime_org_select" in verify
    assert "runtime_role_select" in verify

    # Schema ownership capability is temporary and verified absent afterward.
    assert "GRANT CREATE ON SCHEMA public TO app_security_owner" in forward
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in forward
    assert "security_owner_create" in verify


def test_legacy_rbac_trigger_ignores_unrelated_lifecycle_updates() -> None:
    forward = _function_source("_create_forward_rbac_guard")

    assert "BEFORE UPDATE OF deleted_at, branch_status ON public.org_branch_state" in forward
    assert "OLD.deleted_at IS DISTINCT FROM NEW.deleted_at" in forward
    assert "OLD.branch_status IS DISTINCT FROM NEW.branch_status" in forward
    assert "OLD.deleted_at IS NOT DISTINCT FROM NEW.deleted_at" in forward
    assert "OLD.branch_status IS NOT DISTINCT FROM NEW.branch_status" in forward


def test_critical_delete_guard_is_scoped_and_does_not_lock_tenant_root() -> None:
    source = _source()
    forward = _function_source("_create_forward_delete_guard")
    verify = _function_source("_verify_forward")

    assert "BEFORE UPDATE OF deleted_at ON public.org_branch_state" in forward
    assert "OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL" in forward
    assert "pg_advisory_xact_lock" in forward
    assert "hashtextextended" in forward
    assert "FROM public.org_branch_state AS branch_state" in forward

    assert "FROM public.organizations" not in forward
    assert "FOR UPDATE" not in forward
    assert "retained tenant-root row locking" in verify

    # The original tenant-root lock survives only in the downgrade restoration.
    predecessor = _function_source("_create_predecessor_objects")
    assert "FROM organizations WHERE id = OLD.org_id FOR UPDATE" in predecessor


def test_branch_soft_delete_cascade_uses_internal_tenant_scoped_authority() -> None:
    source = _source()
    forward = _function_source("_create_forward_branch_cascade")
    verify = _function_source("_verify_forward")

    assert "CREATE FUNCTION public.cascade_branch_soft_delete_runtime()" in forward
    assert "SECURITY DEFINER" in forward
    assert "SET search_path = pg_catalog, public" in forward
    assert "SET row_security = on" in forward
    assert "current_setting('app.current_org_id', true)" in forward
    assert "Branch cascade tenant mismatch" in forward

    # Only the no-login security owner receives the dependent-data capability.
    assert "UPDATE (deleted_at) ON TABLE public.branch_operating_hours TO app_security_owner" in forward
    assert "UPDATE (deleted_at) ON TABLE public.branch_special_hours TO app_security_owner" in forward
    assert "DELETE ON TABLE public.branch_hours_projection TO app_security_owner" in forward
    assert "UPDATE (deleted_at) ON TABLE public.branch_operating_hours TO app_runtime" not in source
    assert "UPDATE (deleted_at) ON TABLE public.branch_special_hours TO app_runtime" not in source
    assert "DELETE ON TABLE public.branch_hours_projection TO app_runtime" not in source

    # FORCE-RLS remains meaningful: internal writes get explicit tenant-scoped
    # policies instead of BYPASSRLS or broad runtime privileges.
    assert "CREATE POLICY internal_branch_hours_soft_delete_update" in forward
    assert "CREATE POLICY internal_branch_special_hours_soft_delete_update" in forward
    assert "CREATE POLICY internal_branch_hours_projection_delete" in forward
    assert "TO app_security_owner" in forward
    assert "app.current_org_id" in forward
    assert "runtime_branch_hours_update" in verify
    assert "runtime_special_hours_update" in verify
    assert "runtime_projection_delete" in verify

    # The trigger is narrowed to the actual soft-delete edge.
    assert "AFTER UPDATE OF deleted_at ON public.org_branch_state" in forward
    assert "WHEN (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)" in forward


def test_downgrade_restores_exact_legacy_trigger_shape_and_acl() -> None:
    source = _source()
    predecessor = _function_source("_create_predecessor_objects")
    drop_forward = _function_source("_drop_forward_objects")
    restore_cascade = _function_source("_restore_predecessor_cascade_trigger")
    verify_predecessor = _function_source("_verify_predecessor")

    assert "BEFORE UPDATE ON public.org_branch_state" in predecessor
    assert "SELECT role::TEXT INTO actor_role FROM gym_owners" in predecessor
    assert "FROM organizations WHERE id = OLD.org_id FOR UPDATE" in predecessor
    assert "REVOKE SELECT (id, org_id, role)" in drop_forward
    assert "FROM app_security_owner" in drop_forward
    assert "downgrade leaked revision-owned branch privileges" in verify_predecessor

    # The untouched DBEB function is rediscovered structurally and its original
    # AFTER UPDATE OF deleted_at trigger shape is restored without CASCADE.
    assert "cascade_branch_soft_delete" in restore_cascade
    assert "UPDATE public.branch_operating_hours" in restore_cascade
    assert "AFTER UPDATE OF deleted_at ON public.org_branch_state" in restore_cascade
    assert "DROP POLICY internal_branch_hours_soft_delete_update" in drop_forward
    assert "DROP POLICY internal_branch_special_hours_soft_delete_update" in drop_forward
    assert "DROP POLICY internal_branch_hours_projection_delete" in drop_forward

    downgrade = _function_source("downgrade")
    assert "_verify_forward(bind)" in downgrade
    assert "_drop_forward_objects()" in downgrade
    assert "_create_predecessor_objects()" in downgrade
    assert "_verify_predecessor(bind)" in downgrade
