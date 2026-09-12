"""Repository guard for every GitHub Actions job that migrates to Alembic HEAD."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cluster_role_contract import load_contract_bundle


WORKFLOWS = ROOT / ".github" / "workflows"
CANONICAL_BOOTSTRAP = "bash scripts/ci/bootstrap_cluster_roles.sh"
STALE_MAINTENANCE_BOOTSTRAP = "provision_lifecycle_maintenance_role.sh"
RETIRED_ROLE = "internal_billing_worker"
TRUSTED_HEAD_WRAPPERS = {
    "bash scripts/ci/prepare_p3e_pg16.sh": "scripts/ci/prepare_p3e_pg16.sh",
}
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


def _managed_role_patterns(
    managed_roles: set[str],
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    managed_pattern = "|".join(re.escape(role) for role in sorted(managed_roles))
    create_pattern = re.compile(
        rf"\bCREATE\s+ROLE\s+({managed_pattern})\b", re.IGNORECASE
    )
    setting_pattern = re.compile(
        rf"\bALTER\s+ROLE\s+({managed_pattern})\s+(?:IN\s+DATABASE\s+\S+\s+)?SET\b",
        re.IGNORECASE,
    )
    membership_pattern = re.compile(
        r"\bGRANT\s+(?:app_rls_executor|app_security_owner)\s+TO\s+migration_owner\b",
        re.IGNORECASE,
    )
    return create_pattern, setting_pattern, membership_pattern


def _command_positions(text: str, command: str) -> list[int]:
    """Return executable-looking literal command positions, ignoring comment-only lines."""

    positions: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("#"):
            search_from = 0
            while True:
                index = line.find(command, search_from)
                if index < 0:
                    break
                positions.append(offset + index)
                search_from = index + len(command)
        offset += len(line)
    return positions


def inspect_head_wrapper_text(
    wrapper_name: str,
    text: str,
    *,
    managed_roles: set[str],
) -> tuple[WorkflowViolation, ...]:
    """Validate a fixed-path wrapper before it can stand in for bootstrap in YAML.

    A trusted wrapper is deliberately narrow: it must itself migrate to HEAD,
    invoke the canonical cluster bootstrap exactly once before the first HEAD
    migration, and must not hand-maintain any canonical managed role or
    migration-owner security membership.
    """

    violations: list[WorkflowViolation] = []
    wrapper_job = "*"

    if STALE_MAINTENANCE_BOOTSTRAP in text:
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.stale_maintenance_bootstrap",
                "trusted HEAD wrapper references the retired lifecycle maintenance bootstrap",
            )
        )
    if RETIRED_ROLE in text:
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.retired_role_reference",
                f"trusted HEAD wrapper references retired role {RETIRED_ROLE}",
            )
        )

    head_positions = [match.start() for match in _HEAD_PATTERN.finditer(text)]
    bootstrap_positions = _command_positions(text, CANONICAL_BOOTSTRAP)

    if not head_positions:
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.head_missing",
                "trusted HEAD wrapper must contain at least one Alembic upgrade head",
            )
        )
    if len(bootstrap_positions) != 1:
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.bootstrap_cardinality",
                "trusted HEAD wrapper must invoke canonical bootstrap exactly once; "
                f"found {len(bootstrap_positions)}",
            )
        )
    elif head_positions and bootstrap_positions[0] > head_positions[0]:
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.bootstrap_order",
                "trusted HEAD wrapper must bootstrap before its first Alembic upgrade head",
            )
        )

    create_pattern, setting_pattern, membership_pattern = _managed_role_patterns(
        managed_roles
    )
    for match in create_pattern.finditer(text):
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.manual_managed_role_create",
                f"trusted HEAD wrapper hand-creates managed role {match.group(1)}",
            )
        )
    for match in setting_pattern.finditer(text):
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.manual_managed_role_setting",
                f"trusted HEAD wrapper hand-maintains managed settings for {match.group(1)}",
            )
        )
    if membership_pattern.search(text):
        violations.append(
            WorkflowViolation(
                wrapper_name,
                wrapper_job,
                "wrapper.manual_migration_membership",
                "trusted HEAD wrapper hand-maintains canonical migration-owner membership edges",
            )
        )

    return tuple(sorted(violations))


def inspect_workflow_text(
    workflow_name: str,
    text: str,
    *,
    managed_roles: set[str],
    trusted_head_wrappers: tuple[str, ...] = (),
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

    create_pattern, setting_pattern, membership_pattern = _managed_role_patterns(
        managed_roles
    )

    for job_name, job_text in _job_blocks(text):
        direct_head_positions = [
            match.start() for match in _HEAD_PATTERN.finditer(job_text)
        ]
        wrapper_positions = [
            position
            for command in trusted_head_wrappers
            for position in _command_positions(job_text, command)
        ]
        # A validated HEAD wrapper is itself a migration-to-HEAD boundary. This
        # closes the historical blind spot where a workflow could hide HEAD in a
        # helper and avoid bootstrap inspection entirely.
        head_positions = sorted(direct_head_positions + wrapper_positions)
        if not head_positions:
            continue

        bootstrap_positions = _command_positions(job_text, CANONICAL_BOOTSTRAP)
        bootstrap_positions.extend(wrapper_positions)
        bootstrap_positions.sort()
        if len(bootstrap_positions) != 1:
            violations.append(
                WorkflowViolation(
                    workflow_name,
                    job_name,
                    "head.bootstrap_cardinality",
                    "HEAD job must invoke canonical bootstrap exactly once, directly "
                    "or through one validated HEAD wrapper; "
                    f"found {len(bootstrap_positions)}",
                )
            )
        elif bootstrap_positions[0] > head_positions[0]:
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


def _validated_head_wrappers(
    root: Path,
    *,
    managed_roles: set[str],
) -> tuple[tuple[str, ...], tuple[WorkflowViolation, ...]]:
    trusted: list[str] = []
    violations: list[WorkflowViolation] = []

    for command, relative_path in sorted(TRUSTED_HEAD_WRAPPERS.items()):
        path = root / relative_path
        if not path.is_file():
            violations.append(
                WorkflowViolation(
                    relative_path,
                    "*",
                    "wrapper.missing",
                    "registered trusted HEAD wrapper does not exist",
                )
            )
            continue
        wrapper_violations = inspect_head_wrapper_text(
            relative_path,
            path.read_text(encoding="utf-8"),
            managed_roles=managed_roles,
        )
        if wrapper_violations:
            violations.extend(wrapper_violations)
            continue
        trusted.append(command)

    return tuple(trusted), tuple(sorted(violations))


def scan_repository(root: Path = ROOT) -> tuple[WorkflowViolation, ...]:
    bundle = load_contract_bundle()
    managed_roles = set(bundle.roles["managed_roles"])
    trusted_head_wrappers, wrapper_violations = _validated_head_wrappers(
        root,
        managed_roles=managed_roles,
    )
    violations: list[WorkflowViolation] = list(wrapper_violations)

    workflows = root / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")):
        violations.extend(
            inspect_workflow_text(
                path.name,
                path.read_text(encoding="utf-8"),
                managed_roles=managed_roles,
                trusted_head_wrappers=trusted_head_wrappers,
            )
        )
    for path in sorted(workflows.glob("*.yaml")):
        violations.extend(
            inspect_workflow_text(
                path.name,
                path.read_text(encoding="utf-8"),
                managed_roles=managed_roles,
                trusted_head_wrappers=trusted_head_wrappers,
            )
        )
    return tuple(sorted(violations))


def main() -> int:
    violations = scan_repository()
    if not violations:
        print(
            "All Alembic HEAD workflow jobs use the canonical external-role bootstrap first"
        )
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
