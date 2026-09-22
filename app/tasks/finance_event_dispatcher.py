from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.celery_app import celery_app
from app.core.database import update_session_context, worker_async_session_maker


logger = logging.getLogger("doers.finance_event_dispatcher")

_BATCH_SIZE = 100
_LEASE_SECONDS = 600
_MAX_BATCHES_PER_RUN = 10
_PROCESS_CONCURRENCY = 8
_MAINTENANCE_TOKEN = "finance_event_delivery"
_SAFE_ERROR = re.compile(r"[^A-Za-z0-9:._/-]+")


def _sqlstate(error: Exception) -> str | None:
    if not isinstance(error, DBAPIError):
        return None
    return (
        getattr(error.orig, "sqlstate", None)
        or getattr(error.orig, "pgcode", None)
    )


def _error_code(error: Exception) -> str:
    state = _sqlstate(error)
    raw = state or type(error).__name__ or "unknown"
    normalized = _SAFE_ERROR.sub("_", raw).strip("_")
    return (normalized or "unknown")[:80]


def _is_permanent(error: Exception) -> bool:
    return _sqlstate(error) in {"22023", "23505", "23514", "42501"}


async def _claim_ready_events(worker_id: uuid.UUID) -> list[dict[str, Any]]:
    async with worker_async_session_maker() as session:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM app_secure.claim_member_subscription_finance_events(
                    CAST(:worker_id AS uuid),
                    CAST(:batch_size AS integer),
                    CAST(:lease_seconds AS integer)
                )
                """
            ),
            {
                "worker_id": worker_id,
                "batch_size": _BATCH_SIZE,
                "lease_seconds": _LEASE_SECONDS,
            },
        )
        events = [dict(row) for row in result.mappings().all()]
        await session.commit()
        return events


async def _install_event_context(
    session,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    await update_session_context(
        session,
        org_id=str(event["organization_id"]),
        trace_id=str(event["finance_event_id"]),
        role="finance_event_worker",
        internal_maintenance=_MAINTENANCE_TOKEN,
        worker_id=str(worker_id),
    )


async def _consume_business_effect(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> dict[str, Any]:
    async with worker_async_session_maker() as session:
        await _install_event_context(session, event=event, worker_id=worker_id)
        result = await session.execute(
            text(
                """
                SELECT *
                FROM app_secure.consume_member_subscription_finance_event(
                    CAST(:finance_event_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:lease_fence AS bigint)
                )
                """
            ),
            {
                "finance_event_id": event["finance_event_id"],
                "worker_id": worker_id,
                "lease_fence": event["lease_fence"],
            },
        )
        consumed = dict(result.mappings().one())
        # PAY-5 atomicity boundary: product mutation + consumed-event evidence
        # commit together. Finance acknowledgement is deliberately separate.
        await session.commit()
        return consumed


async def _acknowledge_finance(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> None:
    async with worker_async_session_maker() as session:
        await _install_event_context(session, event=event, worker_id=worker_id)
        acknowledged = await session.scalar(
            text(
                """
                SELECT app_secure.acknowledge_member_subscription_finance_event(
                    CAST(:finance_event_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:lease_fence AS bigint)
                )
                """
            ),
            {
                "finance_event_id": event["finance_event_id"],
                "worker_id": worker_id,
                "lease_fence": event["lease_fence"],
            },
        )
        if acknowledged is not True:
            raise RuntimeError("PAY-5 Finance acknowledgement returned false")
        await session.commit()


async def _release_failed_delivery(
    event: dict[str, Any],
    worker_id: uuid.UUID,
    error: Exception,
) -> str:
    async with worker_async_session_maker() as session:
        await _install_event_context(session, event=event, worker_id=worker_id)
        status = await session.scalar(
            text(
                """
                SELECT app_secure.release_member_subscription_finance_event(
                    CAST(:finance_event_id AS uuid),
                    CAST(:worker_id AS uuid),
                    CAST(:lease_fence AS bigint),
                    CAST(:error_code AS text),
                    CAST(:permanent AS boolean)
                )
                """
            ),
            {
                "finance_event_id": event["finance_event_id"],
                "worker_id": worker_id,
                "lease_fence": event["lease_fence"],
                "error_code": _error_code(error),
                "permanent": _is_permanent(error),
            },
        )
        await session.commit()
        return str(status)


async def _process_claimed_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> str:
    try:
        consumed = await _consume_business_effect(event, worker_id)
    except Exception as exc:
        try:
            status = await _release_failed_delivery(event, worker_id, exc)
        except Exception as release_error:
            logger.warning(
                "PAY-5 Finance-event failure could not release current lease",
                extra={
                    "finance_event_id": str(event["finance_event_id"]),
                    "worker_id": str(worker_id),
                    "lease_fence": int(event["lease_fence"]),
                    "consume_error": _error_code(exc),
                    "release_error": _error_code(release_error),
                },
            )
            return "lease_lost"

        logger.warning(
            "PAY-5 Finance-event business consumption failed",
            extra={
                "finance_event_id": str(event["finance_event_id"]),
                "worker_id": str(worker_id),
                "lease_fence": int(event["lease_fence"]),
                "status": status,
                "error_code": _error_code(exc),
            },
        )
        return "failed" if status == "failed" else "retry"

    try:
        await _acknowledge_finance(event, worker_id)
    except Exception as exc:
        # The business mutation and consumption journal may already be durable.
        # Never issue a compensating mutation or mark the event pending blindly.
        # Lease expiry/reclaim will replay the consumption record and then ack.
        logger.warning(
            "PAY-5 product commit succeeded but Finance acknowledgement is pending",
            extra={
                "finance_event_id": str(event["finance_event_id"]),
                "worker_id": str(worker_id),
                "lease_fence": int(event["lease_fence"]),
                "consumption_id": str(consumed["consumption_id"]),
                "ack_error": _error_code(exc),
            },
        )
        return "ack_pending"

    logger.info(
        "PAY-5 Finance event delivered",
        extra={
            "finance_event_id": str(event["finance_event_id"]),
            "worker_id": str(worker_id),
            "lease_fence": int(event["lease_fence"]),
            "subscription_term_id": str(consumed["subscription_term_id"]),
            "subscription_status": consumed["subscription_status"],
            "effect_applied": bool(consumed["effect_applied"]),
            "replayed": bool(consumed["replayed"]),
        },
    )
    return "delivered"


async def _poll_finance_events() -> dict[str, int]:
    """Drain bounded batches so one Beat tick can absorb a short payment burst."""

    worker_id = uuid.uuid4()
    summary = {
        "claimed": 0,
        "delivered": 0,
        "retry": 0,
        "failed": 0,
        "ack_pending": 0,
        "lease_lost": 0,
        "batches": 0,
    }
    semaphore = asyncio.Semaphore(_PROCESS_CONCURRENCY)

    async def process(event: dict[str, Any]) -> str:
        async with semaphore:
            return await _process_claimed_event(event, worker_id)

    for _ in range(_MAX_BATCHES_PER_RUN):
        events = await _claim_ready_events(worker_id)
        if not events:
            break

        summary["batches"] += 1
        summary["claimed"] += len(events)
        outcomes = await asyncio.gather(*(process(event) for event in events))
        for outcome in outcomes:
            if outcome in summary:
                summary[outcome] += 1

        if len(events) < _BATCH_SIZE:
            break

    if summary["claimed"]:
        logger.info(
            "PAY-20 Finance-event drain completed",
            extra={"worker_id": str(worker_id), **summary},
        )
    return summary


@celery_app.task(name="app.tasks.finance_event_dispatcher.run")
def run() -> dict[str, int]:
    return asyncio.run(_poll_finance_events())
