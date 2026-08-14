from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.database import update_session_context, worker_async_session_maker
from app.services.branch_lifecycle_service import BranchLifecycleService


logger = logging.getLogger("doers.branch_lifecycle_outbox")

_BATCH_SIZE = 20
_LEASE_SECONDS = 600
_MAINTENANCE_TOKEN = "branch_lifecycle_saga"
_EXTERNAL_EVENT_TYPES = {
    "branch.search_deindex",
    "branch.search_index",
    "branch.member_notification",
    "branch.refund_required",
}


async def _claim_events(worker_id: uuid.UUID) -> list[dict[str, Any]]:
    """Lease pending work and reclaim expired processing rows atomically."""

    async with worker_async_session_maker() as session:
        result = await session.execute(
            text(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM public.branch_outbox_events
                    WHERE attempt_count < max_attempts
                      AND process_after <= pg_catalog.clock_timestamp()
                      AND (
                            status = 'pending'
                            OR (
                                status = 'processing'
                                AND leased_until <= pg_catalog.clock_timestamp()
                            )
                      )
                    ORDER BY process_after, created_at, outbox_id
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE public.branch_outbox_events AS outbox_data
                SET status = 'processing',
                    attempt_count = outbox_data.attempt_count + 1,
                    last_attempted_at = pg_catalog.clock_timestamp(),
                    last_error = NULL,
                    leased_by = :worker_id,
                    leased_until = pg_catalog.clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second')
                FROM candidates
                WHERE outbox_data.outbox_id = candidates.outbox_id
                RETURNING
                    outbox_data.outbox_id,
                    outbox_data.tenant_id,
                    outbox_data.branch_id,
                    outbox_data.event_type,
                    outbox_data.payload,
                    outbox_data.attempt_count,
                    outbox_data.max_attempts,
                    outbox_data.correlation_id
                """
            ),
            {
                "worker_id": worker_id,
                "lease_seconds": _LEASE_SECONDS,
                "batch_size": _BATCH_SIZE,
            },
        )
        events = [dict(row) for row in result.mappings().all()]
        await session.commit()
        return events


async def _install_saga_context(
    session,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    await update_session_context(
        session,
        org_id=str(event["tenant_id"]),
        trace_id=str(event["correlation_id"]),
        role="branch_lifecycle_worker",
        internal_maintenance=_MAINTENANCE_TOKEN,
        worker_id=str(worker_id),
    )


async def _mark_delivered(
    session,
    *,
    outbox_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> None:
    result = await session.execute(
        text(
            """
            UPDATE public.branch_outbox_events
            SET status = 'delivered',
                leased_by = NULL,
                leased_until = NULL,
                last_error = NULL
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
            """
        ),
        {"outbox_id": outbox_id, "worker_id": worker_id},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"Lost lifecycle outbox lease before delivery: {outbox_id}"
        )


async def _process_saga_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    payload = event["payload"] or {}
    try:
        branch_id = uuid.UUID(str(payload["branch_id"]))
        org_id = uuid.UUID(str(payload["org_id"]))
        from_status = str(payload["from_status"])
        to_status = str(payload["to_status"])
        actor_id = uuid.UUID(str(payload["actor_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        return await _fail_event(
            event,
            worker_id,
            ValueError(f"Malformed lifecycle saga payload: {exc}"),
            permanent=True,
        )

    if branch_id != event["branch_id"] or org_id != event["tenant_id"]:
        return await _fail_event(
            event,
            worker_id,
            ValueError("Lifecycle saga payload does not match persisted tenant/branch"),
            permanent=True,
        )

    try:
        async with worker_async_session_maker() as session:
            await _install_saga_context(session, event=event, worker_id=worker_id)
            service = BranchLifecycleService(session)
            await service.execute_saga_cascade(
                branch_id=branch_id,
                org_id=org_id,
                from_status=from_status,
                to_status=to_status,
                correlation_id=event["correlation_id"],
                actor_id=actor_id,
                parent_outbox_id=event["outbox_id"],
                worker_id=worker_id,
            )
            await _mark_delivered(
                session,
                outbox_id=event["outbox_id"],
                worker_id=worker_id,
            )
            # Transaction-B DB effects, durable child commands and the parent
            # delivered marker commit together. A crash before commit leaves no
            # partial B state and the lease becomes reclaimable after expiry.
            await session.commit()
        return "delivered"
    except Exception as exc:
        return await _fail_event(event, worker_id, exc, permanent=False)


async def _fail_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    error: Exception,
    *,
    permanent: bool,
) -> str:
    attempts = int(event["attempt_count"])
    max_attempts = int(event["max_attempts"])
    exhausted = permanent or attempts >= max_attempts
    error_text = f"{type(error).__name__}: {error}"[:2000]

    if exhausted and event["event_type"] == "branch.lifecycle_saga" and not permanent:
        payload = event["payload"] or {}
        try:
            async with worker_async_session_maker() as session:
                await _install_saga_context(session, event=event, worker_id=worker_id)
                service = BranchLifecycleService(session)
                await service.compensate_saga_from_dead_letter(
                    branch_id=uuid.UUID(str(payload["branch_id"])),
                    org_id=uuid.UUID(str(payload["org_id"])),
                    from_status=str(payload["from_status"]),
                    to_status=str(payload["to_status"]),
                    correlation_id=event["correlation_id"],
                    actor_id=uuid.UUID(str(payload["actor_id"])),
                    parent_outbox_id=event["outbox_id"],
                    worker_id=worker_id,
                )
                result = await session.execute(
                    text(
                        """
                        UPDATE public.branch_outbox_events
                        SET status = 'dead_lettered',
                            leased_by = NULL,
                            leased_until = NULL,
                            last_error = :last_error
                        WHERE outbox_id = :outbox_id
                          AND status = 'processing'
                          AND leased_by = :worker_id
                        """
                    ),
                    {
                        "outbox_id": event["outbox_id"],
                        "worker_id": worker_id,
                        "last_error": error_text,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError("Lost lifecycle lease during compensation")
                await session.commit()
            logger.error(
                "Lifecycle saga exhausted retries and Transaction A was compensated",
                extra={
                    "outbox_id": str(event["outbox_id"]),
                    "branch_id": str(event["branch_id"]),
                    "correlation_id": str(event["correlation_id"]),
                    "attempt": attempts,
                },
                exc_info=error,
            )
            return "dead_lettered_compensated"
        except Exception as compensation_error:
            # Never leave attempt=max in processing forever. Dead-letter with
            # explicit compensation failure; the frozen branch remains visible
            # to watchdog/operators for manual repair.
            error_text = (
                f"{error_text}; compensation_failed="
                f"{type(compensation_error).__name__}: {compensation_error}"
            )[:2000]
            logger.exception(
                "Lifecycle saga compensation failed after retry exhaustion",
                extra={"outbox_id": str(event["outbox_id"])},
            )

    if exhausted:
        sql = """
            UPDATE public.branch_outbox_events
            SET status = 'dead_lettered',
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
        """
        outcome = "dead_lettered"
        params: dict[str, Any] = {
            "outbox_id": event["outbox_id"],
            "worker_id": worker_id,
            "last_error": error_text,
        }
    else:
        delay_seconds = min(1800, 30 * (2 ** max(attempts - 1, 0)))
        sql = """
            UPDATE public.branch_outbox_events
            SET status = 'pending',
                process_after = pg_catalog.clock_timestamp()
                    + (:delay_seconds * INTERVAL '1 second'),
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE outbox_id = :outbox_id
              AND status = 'processing'
              AND leased_by = :worker_id
        """
        outcome = "retry"
        params = {
            "outbox_id": event["outbox_id"],
            "worker_id": worker_id,
            "last_error": error_text,
            "delay_seconds": delay_seconds,
        }

    async with worker_async_session_maker() as failure_session:
        result = await failure_session.execute(text(sql), params)
        await failure_session.commit()
    if result.rowcount != 1:
        logger.warning(
            "Unable to release lifecycle event because lease ownership changed",
            extra={
                "outbox_id": str(event["outbox_id"]),
                "worker_id": str(worker_id),
            },
        )
        return "lease_lost"

    log_method = logger.error if exhausted else logger.warning
    log_method(
        "Lifecycle outbox processing failed",
        extra={
            "outbox_id": str(event["outbox_id"]),
            "event_type": event["event_type"],
            "tenant_id": str(event["tenant_id"]),
            "branch_id": str(event["branch_id"]),
            "attempt": attempts,
            "max_attempts": max_attempts,
            "outcome": outcome,
        },
        exc_info=error,
    )
    return outcome


async def _process_external_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    """Fail closed until a real integration handler is wired.

    Logging is not delivery. Search, notification and refund commands remain
    retryable/dead-lettered until an actual downstream integration acknowledges
    them.
    """

    return await _fail_event(
        event,
        worker_id,
        RuntimeError(
            f"No production handler is configured for {event['event_type']}"
        ),
        permanent=False,
    )


async def _process_event(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    event_type = event["event_type"]
    if event_type == "branch.lifecycle_saga":
        return await _process_saga_event(event, worker_id)
    if event_type in _EXTERNAL_EVENT_TYPES:
        return await _process_external_event(event, worker_id)
    return await _fail_event(
        event,
        worker_id,
        ValueError(f"Unsupported lifecycle outbox event type: {event_type}"),
        permanent=True,
    )


async def _poll_outbox() -> dict[str, int]:
    worker_id = uuid.uuid4()
    events = await _claim_events(worker_id)
    summary = {
        "claimed": len(events),
        "delivered": 0,
        "retry": 0,
        "dead_lettered": 0,
        "dead_lettered_compensated": 0,
        "lease_lost": 0,
    }

    for event in events:
        outcome = await _process_event(event, worker_id)
        if outcome in summary:
            summary[outcome] += 1

    if events:
        logger.info(
            "Lifecycle outbox poll completed",
            extra={"worker_id": str(worker_id), **summary},
        )
    return summary


@celery_app.task(name="app.tasks.branch_outbox_poller.run")
def run() -> dict[str, int]:
    return asyncio.run(_poll_outbox())
