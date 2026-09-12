from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path("app/services/branch_lifecycle_service.py")
WORKER_SOURCE = Path("app/tasks/branch_outbox_poller.py")
SEARCH_EVIDENCE_MIGRATION = Path(
    "alembic/versions/u07d8e9f0a35_p4b_search_external_evidence.py"
)
SERVICE_CLASS = "BranchLifecycleService"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _worker_source() -> str:
    return WORKER_SOURCE.read_text(encoding="utf-8")


def _search_evidence_source() -> str:
    return SEARCH_EVIDENCE_MIGRATION.read_text(encoding="utf-8")


def _method_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(_source(), filename=str(SOURCE))
    classes = [
        item
        for item in module.body
        if isinstance(item, ast.ClassDef) and item.name == SERVICE_CLASS
    ]
    assert len(classes) == 1, (
        f"expected exactly one {SERVICE_CLASS} class, found {len(classes)}"
    )
    matches = [
        item
        for item in classes[0].body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {SERVICE_CLASS}.{name} method, found {len(matches)}"
    )
    return matches[0]


def _method_source(name: str) -> str:
    """Return one concrete BranchLifecycleService method from source."""
    source = _source()
    node = _method_node(name)
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _method_executable_source(name: str) -> str:
    """Return executable method body only, excluding its descriptive docstring."""
    node = _method_node(name)
    body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
    return "\n".join(ast.unparse(statement) for statement in body)


def _worker_function_source(name: str) -> str:
    source = _worker_source()
    module = ast.parse(source, filename=str(WORKER_SOURCE))
    matches = [
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    ]
    assert len(matches) == 1, (
        f"expected exactly one worker function named {name}, found {len(matches)}"
    )
    node = matches[0]
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _contains_for_update(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child) == "with_for_update"
        for child in ast.walk(node)
    )


def _contains_org_branch_state_select(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or _call_name(child) != "select":
            continue
        if any(
            isinstance(arg, ast.Name) and arg.id == "OrgBranchState"
            for arg in child.args
        ):
            return True
    return False


def _literal_strings(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def test_transition_authorizes_from_plain_read_before_write_locking() -> None:
    node = _method_node("initiate_transition")
    transition = _method_source("initiate_transition")

    plain_reads = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Assign)
        and _contains_org_branch_state_select(child.value)
        and not _contains_for_update(child.value)
    ]
    assert plain_reads, "transition must perform a tenant-visible plain state read"
    visible_read_line = min(child.lineno for child in plain_reads)

    authorization_checks = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.If)
        and "actor_role" in ast.unparse(child.test)
        and "allowed_roles" in ast.unparse(child.test)
    ]
    assert len(authorization_checks) == 1
    authorization_line = authorization_checks[0].lineno

    advisory_lock_calls = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and any(
            "pg_catalog.pg_advisory_xact_lock" in value
            for value in _literal_strings(child)
        )
    ]
    assert len(advisory_lock_calls) >= 2, (
        "organization and branch advisory locks must both remain present"
    )
    advisory_lock_line = min(child.lineno for child in advisory_lock_calls)

    write_locks = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _call_name(child) == "with_for_update"
    ]
    assert len(write_locks) == 1
    write_lock_line = write_locks[0].lineno

    assert visible_read_line < authorization_line < advisory_lock_line < write_lock_line
    assert "status_code=status.HTTP_403_FORBIDDEN" in transition
    assert "branch_state.status != from_status" in transition
    assert "status_code=status.HTTP_409_CONFLICT" in transition
    assert "Branch status changed concurrently" in transition


def test_optional_booking_surface_is_checked_before_update_without_swallowing_errors() -> None:
    saga = _method_source("execute_saga_cascade")
    worker = _worker_function_source("_process_saga_event")

    existence_probe = "SELECT pg_catalog.to_regclass('public.bookings') IS NOT NULL"
    update_sql = "UPDATE public.bookings"

    assert existence_probe in saga
    assert "relation_exists = await self.db.scalar" in saga
    assert "if relation_exists:" in saga
    assert update_sql in saga
    assert saga.index(existence_probe) < saga.index("if relation_exists:") < saga.index(update_sql)

    assert "except Exception" not in saga
    assert "await self.db.rollback()" not in saga
    assert "_compensate_saga" not in saga

    assert "await service.execute_saga_cascade(" in worker
    assert "await _mark_delivered(" in worker
    assert "await session.commit()" in worker
    assert worker.index("await service.execute_saga_cascade(") < worker.index(
        "await _mark_delivered("
    ) < worker.index("await session.commit()")
    assert "except Exception as exc:" in worker
    assert "return await _fail_event(event, worker_id, exc, permanent=False)" in worker


def test_reconciliation_delegates_bounded_claiming_to_p4b_database_capability() -> None:
    reconcile = _method_source("run_reconciliation_sweep")
    executable = _method_executable_source("run_reconciliation_sweep")
    migration = _search_evidence_source()
    capability = migration.split(
        "CREATE FUNCTION app_secure.enqueue_branch_search_reconciliation", 1
    )[1].split("$function$;", 1)[0]

    # P4B moved cross-tenant discovery/locking into one maintenance-only
    # SECURITY DEFINER capability. Python must not independently rescan and
    # mutate branch search truth or reintroduce per-row transaction ownership.
    assert "app_secure.enqueue_branch_search_reconciliation" in reconcile
    assert '{"batch_size": 100}' in reconcile
    assert "await self.db.commit()" in reconcile
    assert "return int(enqueued_count or 0)" in reconcile
    assert "begin_nested" not in executable
    assert "search_last_synced_at" not in executable
    assert "search_provider_ack_version" not in executable

    # The database capability is bounded and concurrency-safe. Existing
    # unresolved search effects suppress duplicate enqueue, and provider success
    # is still established only by the separate evidence-backed worker path.
    assert "LIMIT p_batch_size" in capability
    assert "FOR UPDATE SKIP LOCKED" in capability
    assert "NOT EXISTS" in capability
    assert "FROM public.branch_outbox_events AS existing" in capability
    assert "INSERT INTO public.branch_outbox_events" in capability
    assert "SELECT count(*)::integer INTO v_count FROM inserted" in capability
    assert "search_last_synced_at" not in capability
    assert "search_provider_ack_version" in capability


def test_watchdog_refuses_missing_transition_timestamp_instead_of_crashing_math() -> None:
    node = _method_node("run_watchdog_sweep")

    guards = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.If)
        and isinstance(child.test, ast.Compare)
        and ast.unparse(child.test).replace(" ", "") == "changed_atisNone"
    ]
    assert len(guards) == 1
    guard = guards[0]

    assert any(isinstance(child, ast.Continue) for child in ast.walk(guard))
    assert any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "logger"
        and child.func.attr in {"warning", "error", "critical"}
        for child in ast.walk(guard)
    )

    duration_assignments = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "duration" for target in child.targets)
        and "now - changed_at" in ast.unparse(child.value)
    ]
    assert len(duration_assignments) == 1
    assert guard.lineno < duration_assignments[0].lineno

    assert not any(
        isinstance(child, ast.Call)
        and _call_name(child) in {"add", "commit", "execute", "flush"}
        for child in ast.walk(guard)
    )
