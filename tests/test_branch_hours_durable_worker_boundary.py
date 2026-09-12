from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "app/models/branch_operating_hours.py"
ROUTER = ROOT / "app/routers/branch_operating_hours.py"
POLLER = ROOT / "app/tasks/outbox_poller.py"
PROJECTION = ROOT / "app/tasks/branch_hours_projection.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_branch_hours_models_have_no_hidden_outbox_callbacks() -> None:
    source = _source(MODEL)
    tree = ast.parse(source)

    assert "transactional_outbox" not in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "listens_for"
        for node in ast.walk(tree)
    )


def test_bulk_replace_is_serialized_and_enqueued_in_same_transaction() -> None:
    source = _source(ROUTER)

    assert "pg_try_advisory_xact_lock" in source
    assert "hashtextextended" in source
    assert "HTTP_409_CONFLICT" in source
    assert "await db.flush()" in source
    assert "enqueue_branch_hours_rebuild" in source
    assert "enqueue_organization_hours_rebuild" in source
    assert "await db.commit()" in source
    assert "transactional_outbox" not in source


def test_poller_uses_atomic_leases_and_no_nested_celery_dispatch() -> None:
    source = _source(POLLER)

    assert "worker_async_session_maker" in source
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "leased_by = :worker_id" in source
    assert "delivery_attempts = outbox_data.delivery_attempts + 1" in source
    assert "enqueue_branch_hours_child" in source
    assert "internal_maintenance=_MAINTENANCE_TOKEN" in source
    assert "worker_id=str(worker_id)" in source
    assert "dead_lettered_at" in source
    assert "2 ** max(attempts - 1, 0)" in source
    assert ".delay(" not in source
    assert "INSERT INTO public.transactional_outbox" not in source


def test_projection_is_transaction_owned_and_has_no_standalone_task() -> None:
    source = _source(PROJECTION)
    tree = ast.parse(source)

    assert "worker_async_session_maker" not in source
    assert "asyncio.run" not in source
    assert "shared_task" not in source
    assert "db.commit" not in source
    assert "rebuild_branch_hours_projection" in source
    assert "ZoneInfo" in source
    assert "_mask_standard_intervals_for_special_day" in source
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run_projection"
        for node in ast.walk(tree)
    )
