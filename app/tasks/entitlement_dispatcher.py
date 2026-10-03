from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.celery_app import celery_app
from app.core.database import (
    entitlement_async_session_maker,
    update_session_context,
)
from app.observability.runtime_metrics import runtime_metrics


_BATCH_SIZE = 100
_LEASE_SECONDS = 600
_MAX_BATCHES = 10
_SAFE_ERROR = re.compile(r"[^A-Za-z0-9:._/-]+")
logger = logging.getLogger("doers.pay24d.entitlement")


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
    async with entitlement_async_session_maker() as session:
        rows = await session.execute(
            text(
                """
                SELECT *
                FROM app_secure.pay24c_claim_entitlement_commands(
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


async def _context(session, command: dict[str, Any], worker_id: uuid.UUID) -> None:
    await update_session_context(
        session,
        org_id=str(command["organization_id"]),
        trace_id=str(command["command_id"]),
        role="entitlement_worker",
        internal_maintenance="pay24c_entitlement",
        worker_id=str(worker_id),
    )


async def _apply(command: dict[str, Any], worker_id: uuid.UUID) -> str:
    async with entitlement_async_session_maker() as session:
        await _context(session, command, worker_id)
        try:
            row = await session.execute(
                text(
                    """
                    SELECT *
                    FROM app_secure.pay24c_apply_entitlement_command(
                        CAST(:command_id AS uuid),
                        CAST(:worker_id AS uuid),
                        CAST(:lease_fence AS bigint)
                    )
                    """
                ),
                {
                    "command_id": command["command_id"],
                    "worker_id": worker_id,
                    "lease_fence": command["lease_fence"],
                },
            )
            row.mappings().one()
            await session.commit()
            return "succeeded"
        except Exception as exc:
            await session.rollback()
            await _context(session, command, worker_id)
            status = await session.scalar(
                text(
                    """
                    SELECT app_secure.pay24c_release_entitlement_command(
                        CAST(:command_id AS uuid),
                        CAST(:worker_id AS uuid),
                        CAST(:lease_fence AS bigint),
                        :error_code,
                        :permanent
                    )
                    """
                ),
                {
                    "command_id": command["command_id"],
                    "worker_id": worker_id,
                    "lease_fence": command["lease_fence"],
                    "error_code": _error_code(exc),
                    "permanent": _permanent(exc),
                },
            )
            await session.commit()
            return str(status)


async def _record_readiness_snapshot() -> None:
    try:
        async with entitlement_async_session_maker() as session:
            row = await session.execute(
                text("SELECT * FROM app_secure.pay24d_entitlement_readiness_snapshot()")
            )
            snapshot = dict(row.mappings().one())
            await session.rollback()
        runtime_metrics().entitlement_snapshot(
            pending=snapshot["pending_count"],
            processing=snapshot["processing_count"],
            failed=snapshot["failed_count"],
            review_required=snapshot["review_required_count"],
            oldest_pending_age_seconds=snapshot["oldest_pending_age_seconds"],
            expired_processing_leases=snapshot["expired_processing_leases"],
        )
    except Exception:
        # Observability never becomes entitlement business authority.
        logger.warning("PAY-24-D readiness snapshot unavailable", exc_info=True)


async def _run() -> dict[str, int]:
    worker_id = uuid.uuid4()
    summary = {"claimed": 0, "succeeded": 0, "pending": 0, "failed": 0}
    for _ in range(_MAX_BATCHES):
        commands = await _claim(worker_id)
        if not commands:
            break
        summary["claimed"] += len(commands)
        for command in commands:
            outcome = await _apply(command, worker_id)
            if outcome in summary:
                summary[outcome] += 1
        if len(commands) < _BATCH_SIZE:
            break
    await _record_readiness_snapshot()
    return summary


@celery_app.task(name="app.tasks.entitlement_dispatcher.run")
def run() -> dict[str, int]:
    return asyncio.run(_run())
