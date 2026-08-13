from __future__ import annotations

from pathlib import Path

from app.core.cluster_role_bootstrap import render_fresh_cluster_bootstrap
from scripts.verify_head_workflow_bootstrap import scan_repository


ROOT = Path(__file__).resolve().parents[1]
CI_BOOTSTRAP = ROOT / "scripts/ci/bootstrap_cluster_roles.sh"
RELEASE_BOOTSTRAP = ROOT / "scripts/release/bootstrap_cluster_roles.sh"
RENDERER = "scripts/render_cluster_role_bootstrap.py"
VERIFIER = "scripts/verify_cluster_role_bootstrap.py"
WORKFLOW_GUARD = "scripts/verify_head_workflow_bootstrap.py"
ROLLOUT_RUNBOOK = ROOT / "docs/preprod-prod-database-rollout.md"


def _executable_source(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_manifest_renderer_is_the_only_canonical_cluster_role_producer() -> None:
    sql = render_fresh_cluster_bootstrap()

    for role in (
        "migration_owner",
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "lifecycle_maintenance_runtime",
        "app_security_owner",
        "app_rls_executor",
    ):
        assert sql.count(f"CREATE ROLE {role} ") == 1

    assert "ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '15s';" in sql
    assert "ALTER ROLE lifecycle_maintenance_runtime SET lock_timeout = '2s';" in sql
    assert (
        "ALTER ROLE lifecycle_maintenance_runtime SET "
        "idle_in_transaction_session_timeout = '30s';"
    ) in sql
    assert "ALTER ROLE lifecycle_maintenance_runtime SET row_security = 'on';" in sql
    assert (
        "GRANT app_rls_executor TO migration_owner WITH ADMIN FALSE, "
        "INHERIT FALSE, SET TRUE;"
    ) in sql
    assert (
        "GRANT app_security_owner TO migration_owner WITH ADMIN FALSE, "
        "INHERIT FALSE, SET TRUE;"
    ) in sql
    assert "internal_billing_worker" in sql
    assert "CREATE ROLE internal_billing_worker" not in sql
    assert "CREATE DATABASE" not in sql
    assert "GRANT CONNECT" not in sql
    assert "app_test_runtime" not in sql


def test_ci_bootstrap_is_fresh_only_and_delegates_to_manifest_renderer() -> None:
    assert CI_BOOTSTRAP.is_file()
    executable = _executable_source(CI_BOOTSTRAP.read_text(encoding="utf-8"))

    assert RENDERER in executable
    assert "sudo -u postgres psql" in executable
    assert "-f \"$SQL_FILE\"" in executable
    assert "CREATE ROLE" not in executable
    assert "ALTER ROLE" not in executable
    assert "GRANT app_" not in executable
    assert "provision_lifecycle_maintenance_role.sh" not in executable


def test_release_bootstrap_uses_ambient_postgres_admin_and_no_embedded_secret() -> None:
    assert RELEASE_BOOTSTRAP.is_file()
    executable = _executable_source(RELEASE_BOOTSTRAP.read_text(encoding="utf-8"))

    assert RENDERER in executable
    assert '--dbname="$ADMIN_DATABASE"' in executable
    assert "sudo" not in executable
    assert "PGPASSWORD=" not in executable
    assert "PASSWORD '" not in executable
    assert "ci-" not in executable
    assert "CREATE ROLE" not in executable
    assert "ALTER ROLE" not in executable


def test_repository_head_jobs_are_converged_on_canonical_bootstrap() -> None:
    assert scan_repository(ROOT) == ()


def test_rollout_runbook_requires_bootstrap_then_read_only_verification_before_head() -> None:
    assert ROLLOUT_RUNBOOK.is_file()
    text = ROLLOUT_RUNBOOK.read_text(encoding="utf-8")

    bootstrap_index = text.index("scripts/release/bootstrap_cluster_roles.sh")
    verify_index = text.index(VERIFIER)
    first_head_index = text.index("alembic -c alembic.ini upgrade head")
    assert bootstrap_index < verify_index < first_head_index

    for token in (
        "fresh-cluster bootstrap",
        "read-only verification",
        "migration_owner",
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "lifecycle_maintenance_runtime",
        "MAINTENANCE_DATABASE_URL",
        "NOINHERIT",
        "NOBYPASSRLS",
        "internal_billing_worker",
        "retired",
        WORKFLOW_GUARD,
        "Production rollback is recovery-driven",
        "Green tests are supporting evidence",
    ):
        assert token in text, f"production rollout runbook lost required contract: {token}"

    assert "provision_lifecycle_maintenance_role.sh" not in text
