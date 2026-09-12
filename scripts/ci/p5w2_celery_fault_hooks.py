"""Test-only process fault hooks for the P5-W2 Celery runtime gate.

This module is loaded explicitly with Celery's ``--include`` option by the
P5-W2 test harness.  Production configuration never imports it.  Durable
business assertions remain PostgreSQL-backed; the files written here are only
process-boundary telemetry and one-shot fault coordination.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from celery.signals import task_prerun, worker_process_init

from app.tasks import branch_outbox_poller, outbox_poller


_TASKS = {
    "app.tasks.branch_outbox_poller.run",
    "app.tasks.outbox_poller.run",
}
_MODES = {
    "",
    "before_db_commit",
    "after_db_commit_before_task_ack",
}
_PATCHED = False


def _absolute_path(environment_name: str) -> Path:
    raw = os.environ.get(environment_name, "").strip()
    if not raw:
        raise RuntimeError(f"P5-W2 fault hook requires {environment_name}")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"P5-W2 {environment_name} must be absolute")
    return path


def _append_telemetry(event: str, **details: Any) -> None:
    path = _absolute_path("P5W2_TELEMETRY_PATH")
    payload = {
        "event": event,
        "pid": os.getpid(),
        "time_ns": time.time_ns(),
        **details,
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise RuntimeError("P5-W2 telemetry write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _take_fault_once() -> bool:
    path = _absolute_path("P5W2_FAULT_SENTINEL")
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        encoded = f"pid={os.getpid()}\n".encode("ascii")
        if os.write(descriptor, encoded) != len(encoded):
            raise RuntimeError("P5-W2 fault sentinel write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _telemetry() -> list[dict[str, Any]]:
    path = _absolute_path("P5W2_TELEMETRY_PATH")
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _wait_at_barrier(task_name: str) -> None:
    expected = int(os.environ.get("P5W2_BARRIER_EXPECTED", "0"))
    if expected <= 0:
        return
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        arrivals = [
            record
            for record in _telemetry()
            if record.get("event") == "task_prerun"
            and record.get("task_name") == task_name
        ]
        if len(arrivals) >= expected:
            return
        time.sleep(0.02)
    raise RuntimeError("P5-W2 concurrent-delivery barrier timed out")


def _targeted(event_id: object) -> bool:
    return str(event_id) == os.environ.get("P5W2_TARGET_EVENT_ID", "")


def _crash(boundary: str, *, event_id: object, worker_id: object, outcome: str = "") -> None:
    _append_telemetry(
        boundary,
        event_id=str(event_id),
        worker_id=str(worker_id),
        outcome=outcome,
    )
    os.kill(os.getpid(), signal.SIGKILL)
    raise AssertionError("SIGKILL unexpectedly returned")


def _wrap_processor(
    original: Callable[[dict[str, Any], Any], Awaitable[str]],
    *,
    id_key: str,
) -> Callable[[dict[str, Any], Any], Awaitable[str]]:
    async def wrapped(event: dict[str, Any], worker_id: Any) -> str:
        event_id = event[id_key]
        mode = os.environ.get("P5W2_FAULT_MODE", "")
        if (
            mode == "before_db_commit"
            and _targeted(event_id)
            and _take_fault_once()
        ):
            # The poller already committed the durable claim before invoking
            # this processor.  Kill before the domain transaction can commit.
            _crash(
                "before_db_commit_kill",
                event_id=event_id,
                worker_id=worker_id,
            )

        outcome = await original(event, worker_id)
        if (
            mode == "after_db_commit_before_task_ack"
            and _targeted(event_id)
            and _take_fault_once()
        ):
            # Both production processors return only after their terminal DB
            # transaction commits.  The Celery task has not returned, so its
            # late broker acknowledgement cannot yet have occurred.
            _crash(
                "after_db_commit_before_task_ack_kill",
                event_id=event_id,
                worker_id=worker_id,
                outcome=outcome,
            )
        return outcome

    return wrapped


@task_prerun.connect
def record_task_process(sender=None, task_id=None, **_kwargs: Any) -> None:
    task_name = getattr(sender, "name", "")
    if task_name not in _TASKS:
        return
    _append_telemetry(
        "task_prerun",
        task_id=str(task_id),
        task_name=task_name,
    )
    _wait_at_barrier(task_name)


@worker_process_init.connect
def install_process_faults(**_kwargs: Any) -> None:
    global _PATCHED
    if _PATCHED:
        return
    mode = os.environ.get("P5W2_FAULT_MODE", "")
    if mode not in _MODES:
        raise RuntimeError(f"unsupported P5-W2 fault mode: {mode!r}")
    _absolute_path("P5W2_TELEMETRY_PATH")
    _absolute_path("P5W2_FAULT_SENTINEL")
    if mode and not os.environ.get("P5W2_TARGET_EVENT_ID", ""):
        raise RuntimeError("P5-W2 process fault requires a target event")

    outbox_poller._process_claimed_event = _wrap_processor(
        outbox_poller._process_claimed_event,
        id_key="id",
    )
    branch_outbox_poller._process_event = _wrap_processor(
        branch_outbox_poller._process_event,
        id_key="outbox_id",
    )
    _PATCHED = True
