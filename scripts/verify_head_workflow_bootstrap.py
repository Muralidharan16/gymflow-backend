"""Repository guard for every GitHub Actions job that migrates to Alembic HEAD."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import re
from dataclasses import dataclass

from app.core.cluster_role_contract import load_contract_bundle


WORKFLOWS = ROOT / ".github" / "workflows"
CANONICAL_BOOTSTRAP = "bash scripts/ci/bootstrap_cluster_roles.sh"
STALE_MAINTENANCE_BOOTSTRAP = "provision_lifecycle_maintenance_role.sh"
RETIRED_ROLE = "internal_billing_worker"
_HEAD_PATTERN = re.compile(r"\balembic\b[^\n]*\bupgrade\s+head\b", re.IGNORECASE)
_JOB_PATTERN = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


@dataclass(frozen=True, order=True)
class WorkflowViolation:
    workflow: str
    job: str
    code: str
    message: str


def _job_blocks(text: str) -> tuple[tuple[str, str], ...]:
    lines = text.splitlines(keepends=True)
    in_jobs = False
    current_name: str | None = None
    current: list[str] = []
    blocks: list[tuple[str, str]] = []

    for line in lines:
        if not in_jobs:
            if line.rstrip() == "jobs:":
                in_jobs = True
            continue

        match = _JOB_PATTERN.match(line)
        if match:
            if current_name is not None:
                blocks.append((current_name, "".join(current)))
            current_name = match.group(1)
            current = [line]
            continue

        if current_name is not None:
            current.append(line)

    if current_name is not None:
        blocks.append((current_name, "".join(current)))
    return tuple(blocks)


def inspect_workflow_text(
    workflow_name: str,
    text: str,
    *,
    managed_roles: set[str],
) -> tuple[WorkflowViolation, ...]:
    violations: list[WorkflowViolation] = []

    if STALE_MAINTENANCE_BOOTSTRAP in text:
        violations.append(
            WorkflowViolation(
                workflow_name,
                "*",
                "stale.maintenance_bootstrap",
                "lifecycle maintenance must be provisioned by the canonical cluster bootstrap",
            )
        )
    if RETIRED_ROLE in text:
        violations.append(
            WorkflowViolation(
                workflow_name,
                "*",
                "retired.role_reference",
                f"retired role {RETIRED_ROLE} must not appear in current workflow bootstrap",
            )
        )

    managed_pattern = "|".join(re.escape(role) for role in sorted(managed_roles))
    create_pattern = re.compile(rf"\bCREATE\s+ROLE\s+({managed_pattern})\b", re.IGNORECASE)
    setting_pattern = re.compile(
        rf"\bALTER\s+ROLE\s+({managed_pattern})\s+(?:IN\s+DATABASE\s+\S+\s+)?SET\b",
        re.IGNORECASE,
    )
    membership_pattern = re.compile(
        r"\bGRANT\s+(?:app_rls_executor|app_security_owner)\s+TO\s+migration_owner\b",
        re.IGNORECASE,
    )

    for job_name, job_text in _job_blocks(text):
        head_matches = list(_HEAD_PATTERN.finditer(job_text))
        if not head_matches:
            continue

        bootstrap_positions = [
            match.start()
            for match in re.finditer(re.escape(CANONICAL_BOOTSTRAP), job_text)
        ]
        if len(bootstrap_positions) != 1:
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.bootstrap_cardinality",
                    f"HEAD job must invoke canonical bootstrap exactly once; found {len(bootstrap_positions)}",
                )
            )
        elif bootstrap_positions[0] > head_matches[0].start():
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.bootstrap_order",
                    "canonical bootstrap must run before the first Alembic upgrade head",
                )
            )

        for match in create_pattern.finditer(job_text):
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.manual_managed_role_create",
                    f"HEAD job hand-creates managed role {match.group(1)}",
                )
            )
        for match in setting_pattern.finditer(job_text):
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.manual_managed_role_setting",
                    f"HEAD job hand-maintains managed settings for {match.group(1)}",
                )
            )
        if membership_pattern.search(job_text):
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.manual_migration_membership",
                    "HEAD job hand-maintains canonical migration-owner membership edges",
                )
            )

    return tuple(sorted(violations))


def scan_repository(root: Path = ROOT) -> tuple[WorkflowViolation, ...]:
    bundle = load_contract_bundle()
    managed_roles = set(bundle.roles["managed_roles"])
    violations: list[WorkflowViolation] = []

    workflows = root / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")):
        violations.extend(
            inspect_workflow_text(
                path.name,
                path.read_text(encoding="utf-8"),
                managed_roles=managed_roles,
            )
        )
    for path in sorted(workflows.glob("*.yaml")):
        violations.extend(
            inspect_workflow_text(
                path.name,
                path.read_text(encoding="utf-8"),
                managed_roles=managed_roles,
            )
        )
    return tuple(sorted(violations))


def main() -> int:
    violations = scan_repository()
    if not violations:
        print("All Alembic HEAD workflow jobs use the canonical external-role bootstrap first")
        return 0

    print("PostgreSQL HEAD workflow bootstrap contract failed:", file=sys.stderr)
    for violation in violations:
        print(
            f" - {violation.workflow}:{violation.job} [{violation.code}] {violation.message}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
