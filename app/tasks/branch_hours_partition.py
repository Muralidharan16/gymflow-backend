from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import text

from app.core.database import worker_async_session_maker


logger = logging.getLogger(__name__)
_PARENT = "public.branch_hours_audit_log"


def _partman_partition_name(moment: datetime) -> str:
    normalized = moment.astimezone(timezone.utc)
    return f"branch_hours_audit_log_p{normalized.year:04d}{normalized.month:02d}01"


def _next_month(moment: datetime) -> datetime:
    normalized = moment.astimezone(timezone.utc)
    if normalized.month == 12:
        return datetime(normalized.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(normalized.year, normalized.month + 1, 1, tzinfo=timezone.utc)


async def verify_audit_partition_readiness() -> None:
    """Verify current/next audit coverage without performing database DDL."""
    now = datetime.now(timezone.utc)
    expected_children = (
        _partman_partition_name(now),
        _partman_partition_name(_next_month(now)),
    )

    async with worker_async_session_maker() as session:
        parent_exists = await session.scalar(
            text("SELECT pg_catalog.to_regclass(:parent_name) IS NOT NULL"),
            {"parent_name": _PARENT},
        )
        if not parent_exists:
            raise RuntimeError("branch-hours audit partition parent is missing")
        missing: list[str] = []
        for child_name in expected_children:
            is_child = await session.scalar(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_inherits AS inheritance
                        WHERE inheritance.inhparent = CAST(:parent_name AS regclass)
                          AND inheritance.inhrelid = pg_catalog.to_regclass(:child_name)
                    )
                """),
                {"parent_name": _PARENT, "child_name": f"public.{child_name}"},
            )
            if not is_child:
                missing.append(child_name)
        default_count = await session.scalar(
            text("""
                SELECT count(*)::integer
                FROM pg_catalog.pg_inherits AS inheritance
                JOIN pg_catalog.pg_class AS child ON child.oid = inheritance.inhrelid
                WHERE inheritance.inhparent = CAST(:parent_name AS regclass)
                  AND pg_catalog.pg_get_expr(child.relpartbound, child.oid, true) = 'DEFAULT'
            """),
            {"parent_name": _PARENT},
        )

    if missing or default_count != 1:
        raise RuntimeError(
            "branch-hours audit partition maintenance is unhealthy: "
            f"missing={missing!r}, default_partitions={default_count!r}"
        )


@shared_task(name="app.tasks.branch_hours_partition.run")
def run() -> None:
    try:
        asyncio.run(verify_audit_partition_readiness())
    except Exception:
        logger.exception("Branch-hours audit partition readiness check failed")
        raise
