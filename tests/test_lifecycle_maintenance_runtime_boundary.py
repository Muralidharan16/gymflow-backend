from __future__ import annotations

import ast
from pathlib import Path

from app.core.cluster_role_bootstrap import render_fresh_cluster_bootstrap


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "app/core/config.py"
DATABASE = ROOT / "app/core/database.py"
TASKS = ROOT / "app/tasks/branch_lifecycle_sweeps.py"
ROUTER = ROOT / "app/routers/branch_lifecycle.py"
MIGRATION = ROOT / "alembic/versions/b5c6d7e8f9a0_bound_lifecycle_maintenance_runtime.py"

API_STATE_COLUMNS = {
    "branch_status",
    "deleted_at",
    "is_active",
    "is_operational",
    "lifecycle_transition_in_progress",
    "saga_compensation_strategy",
    "saga_last_checkpoint",
    "status",
    "status_changed_at",
    "status_changed_by",
    "status_reason",
    "transition_source",
}
MAINTENANCE_STATE_COLUMNS = {
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
    "search_last_synced_at",
    "search_sync_failed_at",
    "search_visibility_version",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _literal_assignment(path: Path, name: str):
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"set", "frozenset"}
                and len(value.args) == 1
                and not value.keywords
            ):
                return set(ast.literal_eval(value.args[0]))
            return ast.literal_eval(value)
    raise AssertionError(f"missing assignment {name}")


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1, f"expected one function {name}, found {len(matches)}"
    return matches[0]


def _executed_sql_fragments(path: Path) -> list[str]:
    """Return string fragments that are actual arguments to SQL execute calls."""
    fragments: list[str] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        for child in ast.walk(node.args[0]):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                fragments.append(child.value)
    return fragments


def test_production_requires_four_distinct_database_identities() -> None:
    source = _source(CONFIG)

    assert 'MAINTENANCE_DATABASE_URL: str = ""' in source
    assert "if not self.MAINTENANCE_DATABASE_URL:" in source
    assert "self.MAINTENANCE_DATABASE_URL in" in source
    assert "self.WORKER_DATABASE_URL" in source
    assert "self.AUTH_DATABASE_URL" in source
    assert "self.DATABASE_URL" in source
    assert "def maintenance_database_url" in source


def test_maintenance_pool_is_nullpooled_and_separate_from_api_worker_pools() -> None:
    source = _source(DATABASE)

    assert "maintenance_async_engine = create_async_engine(" in source
    assert "settings.maintenance_database_url" in source
    assert "poolclass=NullPool" in source
    assert "MaintenanceAsyncSessionLocal = async_sessionmaker(" in source
    assert "maintenance_async_session_maker = MaintenanceAsyncSessionLocal" in source


def test_lifecycle_sweeps_use_only_maintenance_session_and_retry_failures() -> None:
    tree = _tree(TASKS)
    imported_database_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.database":
            imported_database_names.update(alias.name for alias in node.names)

    assert imported_database_names == {
        "maintenance_async_session_maker",
        "update_session_context",
    }

    source = _source(TASKS)
    assert 'internal_maintenance=_MAINTENANCE_CONTEXT' in source
    assert '_MAINTENANCE_CONTEXT = "lifecycle"' in source
    assert "random" not in source
    assert "asyncio.sleep" not in source
    assert "autoretry_for=(Exception,)" in source
    assert "retry_backoff=True" in source
    assert "max_retries=5" in source

    for name in ("_run_watchdog_sweep", "_run_reconciliation_sweep"):
        function = _function(TASKS, name)
        raises = [node for node in ast.walk(function) if isinstance(node, ast.Raise)]
        assert raises, f"{name} must re-raise maintenance failure"


def test_http_maintenance_endpoints_authorize_and_enqueue_without_request_db() -> None:
    tree = _tree(ROUTER)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for name, task_name in (
        ("trigger_watchdog_sweep", "run_watchdog"),
        ("trigger_reconciliation_sweep", "run_reconciliation"),
    ):
        function = functions[name]
        arg_names = {arg.arg for arg in function.args.args}
        assert "db" not in arg_names
        source = ast.get_source_segment(_source(ROUTER), function) or ""
        assert "_require_maintenance_operator(current_staff)" in source
        assert f"from app.tasks.branch_lifecycle_sweeps import {task_name}" in source
        assert f"task = {task_name}.delay()" in source
        assert '"task_id": task.id' in source
        assert "run_watchdog_sweep" not in source
        assert "run_reconciliation_sweep" not in source

    router_source = _source(ROUTER)
    assert 'status_code=status.HTTP_202_ACCEPTED' in router_source


def test_revision_91_is_single_head_and_never_manages_cluster_roles() -> None:
    assert _literal_assignment(MIGRATION, "revision") == "b5c6d7e8f9a0"
    assert _literal_assignment(MIGRATION, "down_revision") == "a4b5c6d7e8f9"

    source = _source(MIGRATION)
    executed_sql = "\n".join(_executed_sql_fragments(MIGRATION)).upper()
    for forbidden_role_ddl in (
        "CREATE ROLE ",
        "ALTER ROLE ",
        "DROP ROLE ",
    ):
        assert forbidden_role_ddl not in executed_sql
    assert "CASCADE" not in executed_sql
    assert "lifecycle_maintenance_runtime" in source

    assert "ROLBYPASSRLS" in executed_sql
    assert "ALTER ROLE" not in executed_sql


def test_revision_91_column_surfaces_are_exact_and_disjoint() -> None:
    assert set(_literal_assignment(MIGRATION, "_API_STATE_COLUMNS")) == API_STATE_COLUMNS
    assert set(_literal_assignment(MIGRATION, "_MAINTENANCE_STATE_COLUMNS")) == MAINTENANCE_STATE_COLUMNS
    assert API_STATE_COLUMNS.isdisjoint(MAINTENANCE_STATE_COLUMNS)

    source = _source(MIGRATION)
    assert "REVOKE UPDATE ON TABLE public.org_branch_state FROM app_runtime" in source
    assert "REVOKE SELECT, INSERT ON TABLE public.branch_watchdog_alerts FROM app_runtime" in source
    assert "GRANT SELECT ON TABLE public.org_branch_state TO lifecycle_maintenance_runtime" in source
    assert "GRANT UPDATE (reconciliation_claimed_at, reconciliation_claimed_by" in source
    assert "GRANT SELECT, INSERT ON TABLE public.branch_watchdog_alerts" in source
    assert "GRANT UPDATE ON TABLE public.org_branch_state TO lifecycle_maintenance_runtime" not in source
    assert "GRANT UPDATE ON TABLE public.organizations TO lifecycle_maintenance_runtime" not in source
    assert "GRANT UPDATE ON TABLE public.branch_outbox_events TO lifecycle_maintenance_runtime" not in source


def test_maintenance_rls_is_context_gated_and_force_rls_is_preconditioned() -> None:
    source = _source(MIGRATION)

    for policy in (
        "lifecycle_maintenance_state_select",
        "lifecycle_maintenance_state_update",
        "lifecycle_maintenance_watchdog_select",
        "lifecycle_maintenance_watchdog_insert",
    ):
        assert policy in source
    assert "current_setting('app.internal_maintenance', true) = 'lifecycle'" in source
    assert "must retain ENABLE + FORCE RLS" in source


def test_canonical_bootstrap_defines_only_safe_maintenance_capability() -> None:
    sql = render_fresh_cluster_bootstrap()

    create_line = next(
        line for line in sql.splitlines()
        if line.startswith("CREATE ROLE lifecycle_maintenance_runtime ")
    )
    assert "NOLOGIN" in create_line
    assert "NOSUPERUSER" in create_line
    assert "NOCREATEDB" in create_line
    assert "NOCREATEROLE" in create_line
    assert "NOINHERIT" in create_line
    assert "NOREPLICATION" in create_line
    assert "NOBYPASSRLS" in create_line
    assert "ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '15s';" in sql
    assert "ALTER ROLE lifecycle_maintenance_runtime SET lock_timeout = '2s';" in sql
    assert (
        "ALTER ROLE lifecycle_maintenance_runtime SET "
        "idle_in_transaction_session_timeout = '30s';"
    ) in sql
    assert "ALTER ROLE lifecycle_maintenance_runtime SET row_security = 'on';" in sql
    assert "GRANT lifecycle_maintenance_runtime TO migration_owner" not in sql
