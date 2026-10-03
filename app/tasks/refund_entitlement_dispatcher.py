from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.celery_app import celery_app
from app.core.database import update_session_context, worker_async_session_maker


_BATCH_SIZE = 100
_LEASE_SECONDS = 600
_SAFE_ERROR = re.compile(r"[^A-Za-z0-9:._/-]+")


def _error_code(error: Exception) -> str:
    raw = (
        getattr(getattr(error, "orig", None), "sqlstate", None)
        if isinstance(error, DBAPIError)
        else None
    ) or type(error).__name__ or "unknown"
    return (_SAFE_ERROR.sub("_", raw).strip("_") or "unknown")[:80]


def _permanent(error: Exception) -> bool:
    code = (
        getattr(getattr(error, "orig", None), "sqlstate", None)
        if isinstance(error, DBAPIError)
        else None
    )
    return code in {"22023", "23505", "23514", "42501"}


async def _claim(worker_id: uuid.UUID) -> list[dict[str, Any]]:
    async with worker_async_session_maker() as session:
        rows = await session.execute(
            text(
                """
                SELECT *
                FROM app_secure.pay24c_claim_refund_events(
                    CAST(:worker_id AS uuid), :batch_size, :lease_seconds
                )
                """
            ),
            {
                "worker_id": worker_id,
                "batch_size": _BATCH_SIZE,
                "lease_seconds": _LEASE_SECONDS,
            },
        )
        result = [dict(row) for row in rows.mappings().all()]
        await session.commit()
        return result


async def _context(session, event: dict[str, Any], worker_id: uuid.UUID) -> None:
    await update_session_context(
        session,
        org_id=str(event["organization_id"]),
        trace_id=str(event["finance_event_id"]),
        role="refund_entitlement_worker",
        internal_maintenance="pay24c_refund_enqueue",
        worker_id=str(worker_id),
    )


async def _process(event: dict[str, Any], worker_id: uuid.UUID) -> str:
    async with worker_async_session_maker() as session:
        await _context(session, event, worker_id)
        try:
            await session.scalar(
                text(
                    """
                    SELECT app_secure.pay24c_consume_refund_event(
                        CAST(:event_id AS uuid),
                        CAST(:worker_id AS uuid),
                        CAST(:fence AS bigint)
                    )
                    """
                ),
                {
                    "event_id": event["finance_event_id"],
                    "worker_id": worker_id,
                    "fence": event["lease_fence"],
                },
            )
            acknowledged = await session.scalar(
                text(
                    """
                    SELECT app_secure.pay24c_ack_refund_event(
                        CAST(:event_id AS uuid),
                        CAST(:worker_id AS uuid),
                        CAST(:fence AS bigint)
                    )
                    """
                ),
                {
                    "event_id": event["finance_event_id"],
                    "worker_id": worker_id,
                    "fence": event["lease_fence"],
                },
            )
            if acknowledged is not True:
                raise RuntimeError("PAY24-C refund entitlement ack returned false")
            await session.commit()
            return "succeeded"
        except Exception as exc:
            await session.rollback()
            await _context(session, event, worker_id)
            status = await session.scalar(
                text(
                    """
                    SELECT app_secure.pay24c_release_refund_event(
                        CAST(:event_id AS uuid),
                        CAST(:worker_id AS uuid),
                        CAST(:fence AS bigint),
                        :error_code,
                        :permanent
                    )
                    """
                ),
                {
                    "event_id": event["finance_event_id"],
                    "worker_id": worker_id,
                    "fence": event["lease_fence"],
                    "error_code": _error_code(exc),
                    "permanent": _permanent(exc),
                },
            )
            await session.commit()
            return str(status)


async def _run() -> dict[str, int]:
    worker_id = uuid.uuid4()
    summary = {"claimed": 0, "succeeded": 0, "pending": 0, "failed": 0}
    events = await _claim(worker_id)
    summary["claimed"] = len(events)
    for event in events:
        outcome = await _process(event, worker_id)
        if outcome in summary:
            summary[outcome] += 1
    return summary


@celery_app.task(name="app.tasks.refund_entitlement_dispatcher.run")
def run() -> dict[str, int]:
    return asyncio.run(_run())
