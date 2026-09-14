"""CI-only deterministic poison injection for the P6-P runtime gate.

Loaded only through Celery's explicit ``--include`` option in P6-P CI.
Production configuration never imports this module. The hook targets a
structurally valid durable lifecycle command carrying the P6-P marker and
forces a deterministic retryable application failure so the real worker,
Redis broker and PostgreSQL retry/dead-letter machinery can be exercised.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from celery.signals import worker_process_init

from app.tasks import branch_outbox_poller


_PATCHED = False


def _wrap_processor(
    original: Callable[[dict[str, Any], Any], Awaitable[str]],
) -> Callable[[dict[str, Any], Any], Awaitable[str]]:
    async def wrapped(event: dict[str, Any], worker_id: Any) -> str:
        payload = event.get("payload") or {}
        if (
            event.get("event_type") == "branch.lifecycle_saga"
            and payload.get("p6p_deterministic_failure") is True
        ):
            return await branch_outbox_poller._fail_event(
                event,
                worker_id,
                RuntimeError("P6-P deterministic valid-command failure"),
                permanent=False,
            )
        return await original(event, worker_id)

    return wrapped


@worker_process_init.connect
def install_poison_fault(**_kwargs: Any) -> None:
    global _PATCHED
    if _PATCHED:
        return
    if os.environ.get("P6P_POISON_FAULTS") != "1":
        raise RuntimeError("P6-P poison hook requires explicit CI enablement")
    branch_outbox_poller._process_event = _wrap_processor(
        branch_outbox_poller._process_event
    )
    _PATCHED = True
