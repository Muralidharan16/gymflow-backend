"""Test-only warm-shutdown hold point for the P6-W Celery runtime gate.

Loaded only with Celery's explicit ``--include`` option by P6-W CI. Production
configuration never imports this module. The hook pauses one targeted durable
lifecycle event after its PostgreSQL lease claim has committed but before the
domain processor runs, allowing CI to send SIGTERM to the *main* worker process
and observe Celery warm shutdown semantics without mocking the worker.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from celery.signals import task_prerun, worker_process_init

from app.tasks import branch_outbox_poller


_PATCHED = False
_TASK_NAME = "app.tasks.branch_outbox_poller.run"


def _absolute_path(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"P6-W SIGTERM hook requires {name}")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"P6-W {name} must be absolute")
    return path


def _append(event: str, **details: Any) -> None:
    path = _absolute_path("P6W_TELEMETRY_PATH")
    payload = {
        "event": event,
        "pid": os.getpid(),
        "time_ns": time.time_ns(),
        **details,
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise RuntimeError("P6-W telemetry write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _take_hold_once() -> bool:
    path = _absolute_path("P6W_HOLD_SENTINEL")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        encoded = f"pid={os.getpid()}\n".encode("ascii")
        if os.write(descriptor, encoded) != len(encoded):
            raise RuntimeError("P6-W hold sentinel write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _targeted(event_id: object) -> bool:
    return str(event_id) == os.environ.get("P6W_TARGET_EVENT_ID", "")


def _wrap_processor(
    original: Callable[[dict[str, Any], Any], Awaitable[str]],
) -> Callable[[dict[str, Any], Any], Awaitable[str]]:
    async def wrapped(event: dict[str, Any], worker_id: Any) -> str:
        event_id = event["outbox_id"]
        if _targeted(event_id) and _take_hold_once():
            release = _absolute_path("P6W_RELEASE_PATH")
            _append(
                "sigterm_hold_entered",
                event_id=str(event_id),
                worker_id=str(worker_id),
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if release.exists():
                    _append(
                        "sigterm_hold_released",
                        event_id=str(event_id),
                        worker_id=str(worker_id),
                    )
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError("P6-W SIGTERM hold timed out waiting for release")
        return await original(event, worker_id)

    return wrapped


@task_prerun.connect
def record_task_process(sender=None, task_id=None, **_kwargs: Any) -> None:
    task_name = getattr(sender, "name", "")
    if task_name != _TASK_NAME:
        return
    _append(
        "task_prerun",
        task_id=str(task_id),
        task_name=task_name,
    )


@worker_process_init.connect
def install_sigterm_hold(**_kwargs: Any) -> None:
    global _PATCHED
    if _PATCHED:
        return
    _absolute_path("P6W_TELEMETRY_PATH")
    _absolute_path("P6W_HOLD_SENTINEL")
    _absolute_path("P6W_RELEASE_PATH")
    if not os.environ.get("P6W_TARGET_EVENT_ID", ""):
        raise RuntimeError("P6-W SIGTERM hook requires P6W_TARGET_EVENT_ID")
    branch_outbox_poller._process_event = _wrap_processor(
        branch_outbox_poller._process_event
    )
    _PATCHED = True
