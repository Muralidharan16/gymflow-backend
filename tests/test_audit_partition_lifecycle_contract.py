import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "ze07d8e9f0a3f_audit_partition_lifecycle.py"
)

TASKS = ROOT / "app" / "tasks" / "platform_maintenance.py"
CELERY = ROOT / "app" / "core" / "celery_app.py"


def _source(path: Path) -> str:
    return path.read_text()


def _string_constants(path: Path) -> set[str]:
    """Return Python's semantic string constants, not raw source fragments."""

    tree = ast.parse(path.read_text())

    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }


def test_audit_partition_migration_is_narrow_and_reversible() -> None:
    source = _source(MIGRATION)
    strings = _string_constants(MIGRATION)

    assert 'revision = "ze07d8e9f0a3f"' in source
    assert 'down_revision = "zd07d8e9f0a3e"' in source

    # migration_owner intentionally has no USAGE on app_secure.
    # Migration introspection therefore must use pg_catalog OIDs
    # rather than schema-resolving regprocedure text.
    assert "pg_catalog.to_regprocedure" not in source
    assert "CAST(:function_oid AS oid)" in source
    assert "def _has_function_execute(" in source

    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_private."
        "raise_immutable_audit_violation() "
        "TO migration_owner"
        in strings
    )

    assert (
        "GRANT CREATE ON SCHEMA public"
        not in source
    )

    assert (
        "GRANT EXECUTE ON FUNCTION "
        "app_secure."
        "maintain_branch_audit_partitions() "
        "TO lifecycle_maintenance_runtime"
        in strings
    )

    assert (
        "REVOKE ALL ON FUNCTION "
        "app_secure."
        "maintain_branch_audit_partitions() "
        "FROM PUBLIC"
        in strings
    )

    assert (
        "maintain_branch_audit_partitions_internal()"
        in source
    )

    assert "FOR v_offset IN 0..2 LOOP" in source
    assert "months => v_offset" in source
    assert "pg_advisory_xact_lock" in source

    assert "ze07 downgrade blocked: " in source

    forbidden_statements = {
        "GRANT CREATE ON SCHEMA public TO app_runtime",
        "GRANT CREATE ON SCHEMA public TO worker_runtime",
        "GRANT CREATE ON SCHEMA public TO auth_runtime",
        (
            "GRANT CREATE ON SCHEMA public "
            "TO lifecycle_maintenance_runtime"
        ),
        (
            "GRANT EXECUTE ON FUNCTION "
            "app_private."
            "maintain_branch_audit_partitions_internal() "
            "TO lifecycle_maintenance_runtime"
        ),
    }

    assert forbidden_statements.isdisjoint(strings)


def test_audit_partition_task_uses_maintenance_control_plane() -> None:
    tasks = _source(TASKS)
    celery = _source(CELERY)
    task_strings = _string_constants(TASKS)

    task_name = (
        "app.tasks.platform_maintenance."
        "maintain_branch_audit_partitions"
    )

    assert task_name in tasks

    assert any(
        "app_secure.maintain_branch_audit_partitions()"
        in value
        for value in task_strings
    )

    assert "maintenance_async_session_maker" in tasks

    assert task_name in celery

    assert (
        '"branch-audit-partition-maintenance"'
        in celery
    )

    assert (
        '"options": {"queue": MAINTENANCE_QUEUE}'
        in celery
    )
