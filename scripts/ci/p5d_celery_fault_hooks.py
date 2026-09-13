"""Test-only Celery broker-loss barrier for P5-D.

Loaded explicitly with Celery ``--include`` by the P5-D runtime harness.  The
wrapper never changes the production outcome.  It only pauses after the real
branch-outbox processor returns (therefore after its durable DB transaction)
and before the Celery task itself can return/ack to Redis.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from celery.signals import worker_process_init

from app.tasks import branch_outbox_poller


_PATCHED = False


def _absolute_path(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"P5-D Celery fault hook requires {name}")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"P5-D {name} must be absolute")
    return path


def _append(event: str, **details: Any) -> None:
    path = _absolute_path("P5D_TELEMETRY_PATH")
    payload = {"event": event, "pid": os.getpid(), "time_ns": time.time_ns(), **details}
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        if os.write(descriptor, encoded) != len(encoded):
            raise RuntimeError("P5-D telemetry write was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _targeted(event_id: object) -> bool:
    return str(event_id) == os.environ.get("P5D_TARGET_EVENT_ID", "")


def _wrap(
    original: Callable[[dict[str, Any], Any], Awaitable[str]],
) -> Callable[[dict[str, Any], Any], Awaitable[str]]:
    async def wrapped(event: dict[str, Any], worker_id: Any) -> str:
        outcome = await original(event, worker_id)
        if (
            os.environ.get("P5D_BROKER_FAULT_MODE") == "after_db_commit_before_task_ack"
            and _targeted(event["outbox_id"])
        ):
            _append(
                "after_db_commit_before_task_ack",
                event_id=str(event["outbox_id"]),
                worker_id=str(worker_id),
                outcome=outcome,
            )
            release = _absolute_path("P5D_RELEASE_PATH")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if release.exists():
                    return outcome
                time.sleep(0.02)
            raise RuntimeError("P5-D broker acknowledgement barrier timed out")
        return outcome

    return wrapped


@worker_process_init.connect
def install_p5d_broker_fault(**_kwargs: Any) -> None:
    global _PATCHED
    if _PATCHED:
        return
    if os.environ.get("P5D_BROKER_FAULT_MODE", "") not in {
        "",
        "after_db_commit_before_task_ack",
    }:
        raise RuntimeError("unsupported P5-D broker fault mode")
    _absolute_path("P5D_TELEMETRY_PATH")
    _absolute_path("P5D_RELEASE_PATH")
    branch_outbox_poller._process_event = _wrap(branch_outbox_poller._process_event)
    _PATCHED = True
