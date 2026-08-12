from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_PROVISIONER = "scripts/ci/provision_lifecycle_maintenance_role.sh"
RELEASE_PROVISIONER = "scripts/release/provision_lifecycle_maintenance_role.sh"
ROLLOUT_RUNBOOK = "docs/preprod-prod-database-rollout.md"

# These workflows intentionally create fresh production-shaped PostgreSQL
# databases and run the Alembic lineage. Cluster-scoped maintenance identity
# provisioning is infrastructure-owned and must happen before Alembic; the
# migration itself must remain unable to create or alter that cluster role.
FRESH_MIGRATION_WORKFLOWS = (
    "migration-lifecycle-ci.yml",
    "hardening-ci.yml",
    "finance-hardening-ci.yml",
    "worker-production-boundary.yml",
    "branch-hours-production-boundary.yml",
    "lifecycle-compensation-production-boundary.yml",
    "lifecycle-maintenance-production-boundary.yml",
)

_JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_UPGRADE_HEAD = re.compile(r"\balembic\b[^\n]*\bupgrade\s+head\b")

_REQUIRED_REDUCED_ROLE_TOKENS = (
    "CREATE ROLE lifecycle_maintenance_runtime",
    "NOLOGIN",
    "NOSUPERUSER",
    "NOCREATEDB",
    "NOCREATEROLE",
    "NOINHERIT",
    "NOREPLICATION",
    "NOBYPASSRLS",
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "row_security",
)


def _job_blocks(workflow_text: str) -> list[tuple[str, str]]:
    lines = workflow_text.splitlines(keepends=True)
    try:
        jobs_index = next(index for index, line in enumerate(lines) if line.strip() == "jobs:")
    except StopIteration as exc:  # pragma: no cover - assertion below gives context
        raise AssertionError("workflow has no jobs mapping") from exc

    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in lines[jobs_index + 1 :]:
        match = _JOB_HEADER.match(line)
        if match:
            if current_name is not None:
                blocks.append((current_name, "".join(current_lines)))
            current_name = match.group(1)
            current_lines = [line]
            continue
        if current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        blocks.append((current_name, "".join(current_lines)))
    return blocks


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _executable_source(text: str) -> str:
    """Return shell/SQL source without comment-only lines.

    Security assertions about executable commands must not trip on explanatory
    comments such as "does not use sudo". Inline SQL and here-doc bodies stay
    intact because they are executable input to psql.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _assert_migration_owner_isolated_from_maintenance(text: str) -> None:
    normalized = _normalized(text)
    for access_mode in ("MEMBER", "SET"):
        predicate = (
            "pg_catalog.pg_has_role( "
            "'migration_owner', "
            "'lifecycle_maintenance_runtime', "
            f"'{access_mode}' )"
        )
        assert predicate in normalized, (
            "maintenance provisioner must fail closed if migration_owner gains "
            f"{access_mode} access to lifecycle_maintenance_runtime"
        )

    # Keep the guard executable/fail-closed without coupling the regression to
    # human-facing exception prose, which may legitimately change.
    assert "RAISE EXCEPTION" in text
    assert "GRANT lifecycle_maintenance_runtime TO migration_owner" not in text


def test_every_fresh_migration_job_provisions_external_maintenance_role_first() -> None:
    checked_jobs: list[str] = []

    for workflow_name in FRESH_MIGRATION_WORKFLOWS:
        workflow_path = WORKFLOW_DIR / workflow_name
        assert workflow_path.is_file(), f"missing production workflow: {workflow_name}"
        workflow_text = workflow_path.read_text(encoding="utf-8")

        for job_name, job_text in _job_blocks(workflow_text):
            upgrades = list(_UPGRADE_HEAD.finditer(job_text))
            if not upgrades:
                continue

            checked_jobs.append(f"{workflow_name}:{job_name}")
            provision_index = job_text.find(CI_PROVISIONER)
            assert provision_index >= 0, (
                f"{workflow_name}:{job_name} runs fresh Alembic without the "
                "infrastructure-owned lifecycle maintenance role provisioner"
            )
            assert provision_index < upgrades[0].start(), (
                f"{workflow_name}:{job_name} provisions lifecycle maintenance "
                "after Alembic has already started"
            )

            # Workflows may verify the cluster role, but creation/alteration is
            # centralized in the infrastructure provisioner rather than copied
            # into migration/deployment jobs.
            assert "CREATE ROLE lifecycle_maintenance_runtime" not in job_text
            assert "ALTER ROLE lifecycle_maintenance_runtime" not in job_text

    assert checked_jobs, "no fresh-migration production jobs were inspected"


def test_ci_lifecycle_maintenance_provisioner_is_fail_closed_and_reduced() -> None:
    provisioner_path = ROOT / CI_PROVISIONER
    assert provisioner_path.is_file(), "CI lifecycle maintenance provisioner is missing"
    text = provisioner_path.read_text(encoding="utf-8")
    normalized = _normalized(text)

    for token in _REQUIRED_REDUCED_ROLE_TOKENS:
        assert token in text, f"CI maintenance provisioner lost contract token: {token}"

    _assert_migration_owner_isolated_from_maintenance(text)
    assert "DROP ROLE lifecycle_maintenance_runtime" not in text
    assert "BYPASSRLS" not in normalized.replace("NOBYPASSRLS", "")


def test_release_lifecycle_maintenance_provisioner_is_idempotent_and_fail_closed() -> None:
    provisioner_path = ROOT / RELEASE_PROVISIONER
    assert provisioner_path.is_file(), "production lifecycle maintenance provisioner is missing"
    text = provisioner_path.read_text(encoding="utf-8")
    normalized = _normalized(text)
    executable = _executable_source(text)

    for token in _REQUIRED_REDUCED_ROLE_TOKENS:
        assert token in text, f"release maintenance provisioner lost contract token: {token}"

    # Production bootstrap must use the operator's approved libpq identity, not
    # CI host privilege escalation or embedded credentials. Inspect executable
    # input rather than documentation comments that may explicitly describe
    # forbidden commands by name.
    assert "sudo" not in executable
    assert "PGPASSWORD=" not in executable
    assert "PASSWORD '" not in executable
    assert "ci-" not in executable
    assert "--dbname=\"$ADMIN_DATABASE\"" in executable
    assert "operator_record.rolsuper OR operator_record.rolcreaterole" in executable

    # Existing safe capability is accepted; unsafe state is rejected before any
    # role-setting mutation. Conditional \gexec makes first creation idempotent.
    preflight_index = executable.index("existing lifecycle_maintenance_runtime has unsafe attributes")
    conditional_create_index = executable.index("WHERE NOT EXISTS (")
    first_alter_index = executable.index("ALTER ROLE lifecycle_maintenance_runtime SET")
    assert preflight_index < first_alter_index
    assert conditional_create_index < first_alter_index
    assert "\\gexec" in executable
    assert "refuse automatic repair" in executable

    # The capability must remain isolated from migration/API/auth/worker/security
    # groups. Incoming membership from a separately managed maintenance login is
    # allowed and is verified independently by the runtime boundary.
    for forbidden_role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
        "app_security_owner",
        "app_rls_executor",
    ):
        assert f"'{forbidden_role}'" in executable
    _assert_migration_owner_isolated_from_maintenance(executable)

    assert "DROP ROLE lifecycle_maintenance_runtime" not in executable
    assert "BYPASSRLS" not in _normalized(executable).replace("NOBYPASSRLS", "")


def test_rollout_runbook_requires_cluster_bootstrap_before_alembic() -> None:
    runbook_path = ROOT / ROLLOUT_RUNBOOK
    assert runbook_path.is_file(), "production database rollout runbook is missing"
    text = runbook_path.read_text(encoding="utf-8")

    release_index = text.index(RELEASE_PROVISIONER)
    first_upgrade_index = text.index("alembic -c alembic.ini upgrade head")
    assert release_index < first_upgrade_index

    required_tokens = (
        "MAINTENANCE_DATABASE_URL",
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "WORKER_DATABASE_URL",
        "migration_owner",
        "lifecycle_maintenance_runtime",
        "NOLOGIN",
        "NOINHERIT",
        "NOBYPASSRLS",
        "ADMIN FALSE",
        "CI-only",
        CI_PROVISIONER,
        "Never use the CI provisioner as a production runbook command",
        "Production rollback is recovery-driven",
        "Green tests are supporting evidence",
    )
    for token in required_tokens:
        assert token in text, f"production rollout runbook lost required contract: {token}"

    # Prevent the stale deployment instruction from returning.
    assert "Create a fresh pre-production database from scratch.\n2. Run `alembic upgrade head`." not in text