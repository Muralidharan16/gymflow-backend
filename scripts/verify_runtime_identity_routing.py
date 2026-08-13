#!/usr/bin/env python3
"""P2D structural guard for database identity routing."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORBIDDEN_TASK_DATABASE_NAMES = frozenset({
    "AsyncSessionLocal",
    "ApiAsyncSessionLocal",
    "api_async_session_maker",
    "async_session_maker",
    "SessionLocal",
    "SyncSessionLocal",
    "ApiSyncSessionLocal",
    "async_engine",
    "sync_engine",
})
WORKER_DATABASE_NAMES = frozenset({
    "WorkerAsyncSessionLocal",
    "worker_async_session_maker",
    "WorkerSyncSessionLocal",
    "worker_async_engine",
    "worker_sync_engine",
    "update_session_context",
})
MAINTENANCE_DATABASE_NAMES = frozenset({
    "MaintenanceAsyncSessionLocal",
    "maintenance_async_session_maker",
    "maintenance_async_engine",
    "update_session_context",
})
MAINTENANCE_TASKS = frozenset({"branch_lifecycle_sweeps.py"})
AUTH_BOUNDARY_ENDPOINTS = frozenset({
    "signup",
    "verify",
    "resend_verification",
    "login",
    "refresh",
})


def _database_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.database":
            names.update(alias.name for alias in node.names)
    return names


def _depends_on(function: ast.AsyncFunctionDef, dependency: str) -> bool:
    for default in (*function.args.defaults, *function.args.kw_defaults):
        if not isinstance(default, ast.Call):
            continue
        if not isinstance(default.func, ast.Name) or default.func.id != "Depends":
            continue
        if not default.args:
            continue
        argument = default.args[0]
        if isinstance(argument, ast.Name) and argument.id == dependency:
            return True
    return False


def verify_repository(root: Path = ROOT) -> tuple[str, ...]:
    violations: list[str] = []
    tasks = root / "app" / "tasks"

    for path in sorted(tasks.glob("*.py")):
        imports = _database_imports(path)
        forbidden = sorted(imports & FORBIDDEN_TASK_DATABASE_NAMES)
        if forbidden:
            violations.append(
                f"{path.relative_to(root)} imports API database identity names: {forbidden!r}"
            )

        if path.name in MAINTENANCE_TASKS:
            disallowed = sorted(
                name for name in imports
                if name in WORKER_DATABASE_NAMES and name != "update_session_context"
            )
            if disallowed:
                violations.append(
                    f"{path.relative_to(root)} mixes worker identity into maintenance: {disallowed!r}"
                )
        else:
            disallowed = sorted(
                name for name in imports
                if name in MAINTENANCE_DATABASE_NAMES and name != "update_session_context"
            )
            if disallowed:
                violations.append(
                    f"{path.relative_to(root)} uses maintenance identity outside the approved maintenance task set: {disallowed!r}"
                )

    auth_path = root / "app" / "routers" / "auth.py"
    auth_tree = ast.parse(auth_path.read_text(encoding="utf-8"), filename=str(auth_path))
    endpoints = {
        node.name: node
        for node in auth_tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    for name in sorted(AUTH_BOUNDARY_ENDPOINTS):
        function = endpoints.get(name)
        if function is None or not _depends_on(function, "get_auth_db"):
            violations.append(f"app/routers/auth.py::{name} must depend on get_auth_db")

    supervisor = (root / "app" / "core" / "supervisor.py").read_text(encoding="utf-8")
    for forbidden in (
        'start_worker("partition_lifecycle"',
        "OutboxPartitionLifecycleManager",
        "run_lifecycle(async_engine)",
    ):
        if forbidden in supervisor:
            violations.append(
                "app/core/supervisor.py retains application-owned partition DDL boundary: "
                f"{forbidden!r}"
            )

    database = (root / "app" / "core" / "database.py").read_text(encoding="utf-8")
    for required in (
        "WORKER_SYNC_DATABASE_URL = settings.worker_database_url",
        "WorkerSyncSessionLocal = sessionmaker(",
        "worker_sync_engine = create_engine(",
    ):
        if required not in database:
            violations.append(
                f"app/core/database.py missing worker sync identity boundary: {required!r}"
            )

    celery_app = (root / "app" / "core" / "celery_app.py").read_text(encoding="utf-8")
    if "RuntimeDatabaseIdentityBootstep" not in celery_app:
        violations.append("app/core/celery_app.py must register the P2D runtime identity bootstep")
    if 'celery_app.steps["worker"].add(RuntimeDatabaseIdentityBootstep)' not in celery_app:
        violations.append("P2D worker bootstep is not attached to the Celery worker blueprint")

    return tuple(violations)


def main() -> int:
    violations = verify_repository()
    if violations:
        print("P2D runtime identity routing guard FAILED", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("P2D runtime identity routing guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
