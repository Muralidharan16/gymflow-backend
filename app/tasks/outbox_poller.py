from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.database import update_session_context, worker_async_session_maker
from app.tasks.branch_hours_projection import rebuild_branch_hours_projection


logger = logging.getLogger("doers.branch_hours_outbox")

UTC = timezone.utc
_BATCH_SIZE = 20
_LEASE_SECONDS = 600
_MAX_ATTEMPTS = 15
_MAINTENANCE_TOKEN = "branch_hours_projection"


async def _claim_ready_events(worker_id: uuid.UUID) -> list[dict[str, Any]]:
    """Atomically lease ready rows across concurrent worker processes."""

    async with worker_async_session_maker() as session:
        result = await session.execute(
            text(
                """
                WITH candidates AS (
                    SELECT id
                    FROM public.transactional_outbox
                    WHERE processed_at IS NULL
                      AND dead_lettered_at IS NULL
                      AND delivery_attempts < :max_attempts
                      AND available_at <= pg_catalog.clock_timestamp()
                      AND (
                            leased_until IS NULL
                            OR leased_until <= pg_catalog.clock_timestamp()
                      )
                    ORDER BY available_at, created_at, id
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE public.transactional_outbox AS outbox_data
                SET leased_by = :worker_id,
                    leased_until = pg_catalog.clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second'),
                    delivery_attempts = outbox_data.delivery_attempts + 1,
                    last_error = NULL
                FROM candidates
                WHERE outbox_data.id = candidates.id
                RETURNING
                    outbox_data.id,
                    outbox_data.tenant_id,
                    outbox_data.branch_id,
                    outbox_data.event_type,
                    outbox_data.payload,
                    outbox_data.correlation_id,
                    outbox_data.delivery_attempts,
                    outbox_data.parent_event_id
                """
            ),
            {
                "worker_id": worker_id,
                "lease_seconds": _LEASE_SECONDS,
                "max_attempts": _MAX_ATTEMPTS,
                "batch_size": _BATCH_SIZE,
            },
        )
        rows = [dict(row) for row in result.mappings().all()]
        await session.commit()
        return rows


async def _install_tenant_worker_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    correlation_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> None:
    await update_session_context(
        session,
        org_id=str(tenant_id),
        trace_id=str(correlation_id),
        role="branch_hours_worker",
        internal_maintenance=_MAINTENANCE_TOKEN,
        worker_id=str(worker_id),
    )


async def _complete_owned_event(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> None:
    result = await session.execute(
        text(
            """
            UPDATE public.transactional_outbox
            SET processed_at = pg_catalog.clock_timestamp(),
                leased_by = NULL,
                leased_until = NULL,
                last_error = NULL
            WHERE id = :event_id
              AND leased_by = :worker_id
              AND processed_at IS NULL
              AND dead_lettered_at IS NULL
            """
        ),
        {"event_id": event_id, "worker_id": worker_id},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"Lost branch-hours outbox lease before completion: event={event_id}"
        )


async def _supersede_unleased_temporal_refreshes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    keep_event_id: uuid.UUID,
) -> None:
    """Bound queue growth after schedule edits without stealing active leases."""

    await session.execute(
        text(
            """
            UPDATE public.transactional_outbox
            SET processed_at = pg_catalog.clock_timestamp(),
                last_error = 'superseded by newer projection rebuild'
            WHERE tenant_id = :tenant_id
              AND branch_id = :branch_id
              AND id <> :keep_event_id
              AND event_type = 'branch_hours.branch_changed'
              AND payload ->> 'reason' = 'temporal_refresh'
              AND processed_at IS NULL
              AND dead_lettered_at IS NULL
              AND (
                    leased_until IS NULL
                    OR leased_until <= pg_catalog.clock_timestamp()
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "keep_event_id": keep_event_id,
        },
    )


async def _schedule_temporal_refresh(
    session: AsyncSession,
    *,
    parent_event_id: uuid.UUID,
    branch_id: uuid.UUID,
    worker_id: uuid.UUID,
    next_refresh_at: datetime | None,
) -> None:
    if next_refresh_at is None:
        return

    # Run just after the boundary so equality/clock granularity cannot rebuild
    # the old state and immediately reschedule the same transition.
    available_at = next_refresh_at.astimezone(UTC) + timedelta(seconds=1)
    await session.execute(
        text(
            """
            SELECT public.enqueue_branch_hours_child(
                :parent_event_id,
                :branch_id,
                :worker_id,
                :available_at,
                'temporal_refresh'
            )
            """
        ),
        {
            "parent_event_id": parent_event_id,
            "branch_id": branch_id,
            "worker_id": worker_id,
            "available_at": available_at,
        },
    )


async def _process_branch_event(
    session: AsyncSession,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    branch_id = event["branch_id"]
    if branch_id is None:
        raise ValueError("branch-hours branch event is missing branch_id")

    tenant_id = event["tenant_id"]
    correlation_id = event["correlation_id"]
    await _install_tenant_worker_context(
        session,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        worker_id=worker_id,
    )

    try:
        rebuild = await rebuild_branch_hours_projection(
            session,
            branch_id,
            tenant_id,
        )
    except LookupError:
        # A stale event after a legitimate branch deactivation/deletion is safe
        # to consume only when the tenant-bound state row proves that condition.
        state = (
            await session.execute(
                text(
                    """
                    SELECT deleted_at, is_active
                    FROM public.org_branch_state
                    WHERE branch_id = :branch_id
                      AND org_id = :tenant_id
                    """
                ),
                {"branch_id": branch_id, "tenant_id": tenant_id},
            )
        ).mappings().one_or_none()
        if state is None or (
            state["deleted_at"] is None and bool(state["is_active"])
        ):
            raise
        await _complete_owned_event(
            session,
            event_id=event["id"],
            worker_id=worker_id,
        )
        return

    await _supersede_unleased_temporal_refreshes(
        session,
        tenant_id=tenant_id,
        branch_id=branch_id,
        keep_event_id=event["id"],
    )
    await _schedule_temporal_refresh(
        session,
        parent_event_id=event["id"],
        branch_id=branch_id,
        worker_id=worker_id,
        next_refresh_at=rebuild.next_refresh_at,
    )
    await _complete_owned_event(
        session,
        event_id=event["id"],
        worker_id=worker_id,
    )


async def _process_organization_event(
    session: AsyncSession,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    if event["branch_id"] is not None:
        raise ValueError("organization branch-hours event unexpectedly has branch_id")

    tenant_id = event["tenant_id"]
    correlation_id = event["correlation_id"]
    await _install_tenant_worker_context(
        session,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        worker_id=worker_id,
    )

    # The request creates one organization event regardless of tenant size.
    # Fan-out stays in this leased worker transaction. Each child is created by
    # the security-owned function, which verifies live parent lease and lineage;
    # retrying the parent is idempotent through parent+branch dedupe.
    await session.execute(
        text(
            """
            SELECT public.enqueue_branch_hours_child(
                :parent_event_id,
                branch_data.id,
                :worker_id,
                pg_catalog.clock_timestamp(),
                'organization_hours_changed'
            )
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            WHERE branch_data.org_id = :tenant_id
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active IS TRUE
            """
        ),
        {
            "parent_event_id": event["id"],
            "worker_id": worker_id,
            "tenant_id": tenant_id,
        },
    )
    await _complete_owned_event(
        session,
        event_id=event["id"],
        worker_id=worker_id,
    )


async def _release_failed_event(
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
    error: Exception,
    permanent: bool,
) -> str:
    attempts = int(event["delivery_attempts"])
    dead_letter = permanent or attempts >= _MAX_ATTEMPTS
    error_text = f"{type(error).__name__}: {error}"[:2000]

    if dead_letter:
        update_sql = """
            UPDATE public.transactional_outbox
            SET dead_lettered_at = pg_catalog.clock_timestamp(),
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE id = :event_id
              AND leased_by = :worker_id
              AND processed_at IS NULL
              AND dead_lettered_at IS NULL
        """
        delay_seconds = None
        outcome = "dead_lettered"
    else:
        delay_seconds = min(900, 30 * (2 ** max(attempts - 1, 0)))
        update_sql = """
            UPDATE public.transactional_outbox
            SET available_at = pg_catalog.clock_timestamp()
                    + (:delay_seconds * INTERVAL '1 second'),
                leased_by = NULL,
                leased_until = NULL,
                last_error = :last_error
            WHERE id = :event_id
              AND leased_by = :worker_id
              AND processed_at IS NULL
              AND dead_lettered_at IS NULL
        """
        outcome = "retry"

    async with worker_async_session_maker() as failure_session:
        params: dict[str, Any] = {
            "event_id": event["id"],
            "worker_id": worker_id,
            "last_error": error_text,
        }
        if delay_seconds is not None:
            params["delay_seconds"] = delay_seconds
        result = await failure_session.execute(text(update_sql), params)
        await failure_session.commit()

    if result.rowcount != 1:
        logger.warning(
            "Unable to release branch-hours event because lease ownership changed",
            extra={
                "event_id": str(event["id"]),
                "worker_id": str(worker_id),
            },
        )
        return "lease_lost"

    log_method = logger.error if dead_letter else logger.warning
    log_method(
        "Branch-hours outbox processing failed",
        extra={
            "event_id": str(event["id"]),
            "tenant_id": str(event["tenant_id"]),
            "event_type": event["event_type"],
            "attempt": attempts,
            "outcome": outcome,
        },
        exc_info=error,
    )
    return outcome


async def _process_claimed_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    event_type = event["event_type"]
    permanent = event_type not in {
        "branch_hours.branch_changed",
        "branch_hours.organization_changed",
    }
    if permanent:
        return await _release_failed_event(
            event=event,
            worker_id=worker_id,
            error=ValueError(f"Unsupported branch-hours outbox event type: {event_type}"),
            permanent=True,
        )

    try:
        async with worker_async_session_maker() as session:
            if event_type == "branch_hours.branch_changed":
                await _process_branch_event(
                    session,
                    event=event,
                    worker_id=worker_id,
                )
            else:
                await _process_organization_event(
                    session,
                    event=event,
                    worker_id=worker_id,
                )
            # Projection/fan-out side effects and the processed marker commit as
            # one transaction. A crash before commit leaves the lease reclaimable.
            await session.commit()
        return "processed"
    except Exception as exc:
        return await _release_failed_event(
            event=event,
            worker_id=worker_id,
            error=exc,
            permanent=False,
        )


async def _poll_outbox() -> dict[str, int]:
    worker_id = uuid.uuid4()
    events = await _claim_ready_events(worker_id)
    summary = {
        "claimed": len(events),
        "processed": 0,
        "retry": 0,
        "dead_lettered": 0,
        "lease_lost": 0,
    }

    for event in events:
        outcome = await _process_claimed_event(event, worker_id)
        if outcome in summary:
            summary[outcome] += 1

    if events:
        logger.info(
            "Branch-hours outbox poll completed",
            extra={"worker_id": str(worker_id), **summary},
        )
    return summary


@celery_app.task(name="app.tasks.outbox_poller.run")
def run() -> dict[str, int]:
    return asyncio.run(_poll_outbox())
