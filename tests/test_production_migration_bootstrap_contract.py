from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
PROVISIONER = "scripts/ci/provision_lifecycle_maintenance_role.sh"

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
            provision_index = job_text.find(PROVISIONER)
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


def test_lifecycle_maintenance_provisioner_is_fail_closed_and_reduced() -> None:
    provisioner_path = ROOT / PROVISIONER
    assert provisioner_path.is_file(), "lifecycle maintenance provisioner is missing"
    text = provisioner_path.read_text(encoding="utf-8")

    required_contract_tokens = (
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
        "pg_has_role('migration_owner', 'lifecycle_maintenance_runtime', 'MEMBER')",
        "pg_has_role('migration_owner', 'lifecycle_maintenance_runtime', 'SET')",
    )
    for token in required_contract_tokens:
        assert token in text, f"maintenance provisioner lost required contract token: {token}"

    assert "DROP ROLE lifecycle_maintenance_runtime" not in text
