from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MIGRATION = ROOT / "alembic" / "versions" / (
    "b4c5d6e7f809_harden_branch_hours_runtime_boundary.py"
)
PARTITION_MIGRATION = ROOT / "alembic" / "versions" / (
    "c5d6e7f8091a_adopt_branch_hours_audit_partitions.py"
)
PARTITION_TASK = ROOT / "app" / "tasks" / "branch_hours_partition.py"
BRANCH_HOURS_API_TEST = ROOT / "tests" / "test_branch_operating_hours_api.py"
MAIN = ROOT / "app" / "main.py"
CELERY = ROOT / "app" / "core" / "celery_app.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    module = ast.parse(_source(path), filename=str(path))
    return {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _function_source(path: Path, name: str) -> str:
    source = _source(path)
    node = _functions(path)[name]
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _literal_text(path: Path) -> str:
    module = ast.parse(_source(path), filename=str(path))
    return "\n".join(
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_branch_hours_runtime_revision_is_forward_only_after_transition_acl() -> None:
    source = _source(RUNTIME_MIGRATION)
    assert 'revision = "b4c5d6e7f809"' in source
    assert 'down_revision = "a3b4c5d6e7f8"' in source


def test_runtime_acl_is_route_bounded_and_audit_insert_is_internal() -> None:
    source = _literal_text(RUNTIME_MIGRATION)
    runtime_grants = _function_source(RUNTIME_MIGRATION, "_grant_runtime_acl")

    for relation in (
        "public.organization_operating_hours",
        "public.branch_operating_hours",
        "public.branch_special_hours",
    ):
        assert (
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {relation} TO app_runtime"
            in runtime_grants
        )

    assert (
        "GRANT SELECT ON TABLE public.branch_hours_projection TO app_runtime"
        in runtime_grants
    )
    assert "GRANT ALL" not in runtime_grants.upper()
    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert privilege not in runtime_grants.upper()
    assert "branch_hours_audit_log" not in runtime_grants

    assert "GRANT INSERT (table_name, record_id, branch_id, operation, changed_by, old_data, new_data)" in source
    assert "TO app_security_owner" in source
    assert "CREATE POLICY internal_branch_hours_audit_insert" in source


def test_branch_hours_rls_matches_application_authorization_contract() -> None:
    source = _literal_text(RUNTIME_MIGRATION)

    for policy in (
        "org_hours_read_active_member",
        "org_hours_insert_owner_admin",
        "org_hours_update_owner_admin",
        "branch_hours_read_active_member",
        "branch_hours_insert_authorized",
        "branch_hours_update_authorized",
        "branch_special_hours_read_active_member",
        "branch_special_hours_insert_authorized",
        "branch_special_hours_update_authorized",
    ):
        assert f"CREATE POLICY {policy}" in source

    assert "membership_status_id = 3" in source
    assert "IN ('owner', 'admin')" in source
    assert "role_assignment.role_id = 3" in source
    assert "role_assignment.revoked_at IS NULL" in source
    assert "role_assignment.deleted_at IS NULL" in source
    assert "branch_state.deleted_at IS NULL" in source
    assert "branch_state.is_active = TRUE" in source
    assert "app.current_org_id" in source
    assert "app.current_user_id" in source
    assert "app.current_role" in source


def test_runtime_revision_preserves_8192_internal_cascade_policies() -> None:
    source = _source(RUNTIME_MIGRATION)
    for policy in (
        "internal_branch_hours_soft_delete_update",
        "internal_branch_special_hours_soft_delete_update",
        "internal_branch_hours_projection_delete",
    ):
        assert source.count(policy) >= 2

    drop_forward = _function_source(RUNTIME_MIGRATION, "_drop_forward_objects")
    for policy in (
        "internal_branch_hours_soft_delete_update",
        "internal_branch_special_hours_soft_delete_update",
        "internal_branch_hours_projection_delete",
    ):
        assert f"DROP POLICY {policy}" not in drop_forward


def test_audit_trigger_is_tenant_bound_security_definer_and_not_public() -> None:
    source = _literal_text(RUNTIME_MIGRATION)
    forward = _function_source(RUNTIME_MIGRATION, "_create_internal_audit_boundary")

    assert "CREATE FUNCTION public.audit_branch_hours_runtime()" in forward
    assert "SECURITY DEFINER" in forward
    assert "SET search_path = pg_catalog, public" in forward
    assert "SET row_security = on" in forward
    assert "Branch-hours audit tenant mismatch" in forward
    assert "app.current_org_id" in forward
    assert "app.current_user_id" in forward
    assert "REVOKE ALL ON FUNCTION public.audit_branch_hours_runtime() FROM PUBLIC" in forward
    assert "ALTER FUNCTION public.audit_branch_hours_runtime() OWNER TO app_security_owner" in forward
    assert "GRANT EXECUTE ON FUNCTION public.audit_branch_hours_runtime() TO app_runtime" not in source


def test_runtime_revision_downgrade_restores_legacy_hours_contract() -> None:
    source = _literal_text(RUNTIME_MIGRATION)
    downgrade = _function_source(RUNTIME_MIGRATION, "downgrade")

    for policy in (
        "tenant_isolation_org_hours",
        "tenant_isolation_read_hours",
        "write_branch_hours_org_admin",
        "write_branch_hours_manager",
    ):
        assert f"CREATE POLICY {policy}" in source

    assert "EXECUTE FUNCTION app_private.audit_branch_hours()" in source
    assert "_verify_forward(bind)" in downgrade
    assert "_drop_forward_objects()" in downgrade
    assert "_restore_predecessor_policies()" in downgrade
    assert "_restore_predecessor_triggers()" in downgrade
    assert "_verify_predecessor(bind)" in downgrade


def test_partition_revision_moves_ddl_to_existing_pg_partman_control_plane() -> None:
    source = _source(PARTITION_MIGRATION)
    literals = _literal_text(PARTITION_MIGRATION)
    dependency = _function_source(PARTITION_MIGRATION, "_require_partman_dependency")
    create_template = _function_source(PARTITION_MIGRATION, "_create_template")
    configure = _function_source(PARTITION_MIGRATION, "_configure_partman")
    coverage = _function_source(PARTITION_MIGRATION, "_require_current_and_future_coverage")
    cleanup = _function_source(PARTITION_MIGRATION, "_remove_partman_management")

    assert 'revision = "c5d6e7f8091a"' in source
    assert 'down_revision = "b4c5d6e7f809"' in source
    assert '_PARTMAN_VERSION = "5.0.1"' in source
    assert '_TEMPLATE_SCHEMA = "public"' in source
    assert '_TEMPLATE_NAME = "branch_hours_audit_log_partman_template"' in source

    assert "has_schema_privilege(current_user, 'partman', 'CREATE')" in dependency
    assert "migration_owner must not gain CREATE on infrastructure-owned partman schema" in dependency
    assert "GRANT CREATE ON SCHEMA partman" not in literals

    assert "CREATE TABLE" in create_template
    assert "INCLUDING ALL" in create_template
    assert "REVOKE ALL ON TABLE" in create_template
    assert "FROM PUBLIC" in create_template
    assert "p_template_table := :template_table" in configure
    assert '"template_table": _TEMPLATE' in source
    assert "partman.config_cleanup" not in source

    assert "partman.create_parent" in literals
    assert "p_interval := '1 month'" in literals
    assert "p_type := 'range'" in literals
    assert "p_premake := 4" in literals
    assert "p_default_table := true" in literals
    assert "p_automatic_maintenance := 'on'" in literals
    assert "infinite_time_partitions = true" in literals
    assert "partman.run_maintenance" in literals

    assert "CURRENT_TIMESTAMP" in coverage
    assert "pg_catalog.current_timestamp" not in coverage

    assert "DELETE FROM partman.part_config WHERE parent_table = :parent_table" in cleanup
    assert "result.rowcount != 1" in cleanup
    assert "pg_partman audit configuration survived bounded cleanup" in cleanup
    assert "DROP TABLE" in cleanup
    assert "RESTRICT" in cleanup
    assert "revision-owned audit template survived downgrade" in cleanup

    assert "SET retention" not in literals
    assert '"retention": None' in source

    assert "branch_hours_audit_log_y2026m05" in source
    assert "branch_hours_audit_log_p20260501" in source
    assert "Refusing destructive c5d6 downgrade" in source
    assert "CASCADE" not in literals


def test_application_partition_task_is_read_only_and_startup_is_ddl_free() -> None:
    task_literals = _literal_text(PARTITION_TASK)
    task_source = _source(PARTITION_TASK)
    main_source = _source(MAIN)
    celery_source = _source(CELERY)

    for verb in (
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
        "TRUNCATE",
        "GRANT ",
        "REVOKE ",
    ):
        assert verb not in task_literals.upper()

    assert "pg_catalog.pg_inherits" in task_literals
    assert "verify_audit_partition_readiness" in task_source
    assert "ensure_audit_partitions" not in task_source
    assert "ensure_audit_partitions" not in main_source
    assert "branch_hours_partition import" not in main_source

    assert '"daily-branch-hours-audit-partition-readiness"' in celery_source
    assert '"app.tasks.branch_hours_partition.run"' in celery_source


def test_branch_hours_api_uses_unique_tenants_not_destructive_cleanup() -> None:
    source = _source(BRANCH_HOURS_API_TEST)

    assert "uuid.uuid4().hex" in source
    assert "branch_hours_api_{tenant_suffix}@example.com" in source
    assert "branch_hours_other_{other_suffix}@example.com" in source
    assert "cleanup_test_database_tables" not in source
    assert "truncate_test_tables" not in source
    assert "TRUNCATE TABLE" not in source.upper()
    assert "CASCADE" not in source.upper()
    assert "AdminTestSessionLocal" not in source
