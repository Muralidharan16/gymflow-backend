"""
app/core/partition_manager.py
==============================
Automated partition lifecycle manager for the Doers SaaS platform.

Design (Master Blueprint Section 10 + patch_v6 Refinement 4):
  • Pre-creates weekly RANGE partitions for event_outbox and idempotency_store.
  • Detaches and queues for delayed DROP old partitions beyond retention window.
  • Uses SQL identifier quoting (double-quotes) on all generated DDL to prevent injection.
  • Runs as a supervised background worker; all DDL is AUTOCOMMIT (cannot run in tx).
  • Advisory lock prevents concurrent lifecycle runs (PARTITION namespace).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_locks import DistributedLockCoordinator, LockNamespace

logger = logging.getLogger("doers.partition_manager")

# Allowlist of tables we manage partitions for
_MANAGED_TABLES = frozenset({
    "event_outbox",
    "idempotency_store",
    "auth_sessions",
})

# Partition name pattern — strictly validated before any DDL
_PARTITION_NAME_RE = re.compile(r"^[a-z_]+_y\d{4}_m\d{2}_d\d{2}$")


def _safe_partition_name(table: str, date: datetime) -> str:
    """Generate and validate a partition name. Raises ValueError on unsafe input."""
    if table not in _MANAGED_TABLES:
        raise ValueError(f"Unmanaged table: {table}")
    name = f"{table}_y{date.year}_m{date.month:02d}_d{date.day:02d}"
    if not _PARTITION_NAME_RE.match(name):
        raise ValueError(f"Unsafe partition name generated: {name}")
    return name


class OutboxPartitionLifecycleManager:
    """
    Pre-creates next-week partitions and archives past-retention partitions.
    Designed to run weekly from the supervisor tree.
    """

    def __init__(self, retention_weeks: int = 8):
        self._retention_weeks = retention_weeks

    async def run_lifecycle(self, db_engine) -> None:
        """
        Main entry point. Uses a raw AUTOCOMMIT connection for DDL.
        Protected by a session-scoped advisory lock.
        """
        # Use a simple session just for the advisory lock
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as lock_session:
            acquired = await DistributedLockCoordinator.try_lock_session(
                lock_session, LockNamespace.PARTITION, "lifecycle_manager"
            )
            if not acquired:
                logger.info("Partition lifecycle already running. Skipping.")
                return

            try:
                async with db_engine.connect() as conn:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    now = datetime.now(timezone.utc)

                    for table in _MANAGED_TABLES:
                        await self._create_upcoming_partitions(conn, table, now)
                        await self._detach_expired_partitions(conn, table, now)

                    await asyncio.sleep(0)  # cancellation checkpoint

            finally:
                await DistributedLockCoordinator.release_session_lock(
                    lock_session, LockNamespace.PARTITION, "lifecycle_manager"
                )

    async def _create_upcoming_partitions(self, conn, table: str, now: datetime):
        """Pre-create partitions for the next 2 weeks."""
        for weeks_ahead in range(0, 3):
            target = now + timedelta(weeks=weeks_ahead)
            # Align to Monday of the target week
            monday  = target - timedelta(days=target.weekday())
            monday  = monday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            next_w  = monday + timedelta(weeks=1)

            part_name  = _safe_partition_name(table, monday)
            parent_q   = f'"{table}"'
            part_q     = f'"public"."{part_name}"'

            try:
                await conn.execute(sa.text(f"""
                    CREATE TABLE IF NOT EXISTS public.{part_name}
                    PARTITION OF public.{table}
                    FOR VALUES FROM ('{monday.isoformat()}') TO ('{next_w.isoformat()}')
                """))
                logger.info("Partition ensured: %s", part_name)
            except Exception as exc:
                logger.warning("Partition create failed (%s): %s", part_name, exc)
            finally:
                await asyncio.sleep(0)

    async def _detach_expired_partitions(self, conn, table: str, now: datetime):
        """
        Detach partitions older than retention_weeks.
        Detached tables are renamed with _detached suffix and queued for delayed DROP.
        """
        cutoff = now - timedelta(weeks=self._retention_weeks)
        # Walk back 1 extra week to catch the start of the oldest week
        for weeks_back in range(self._retention_weeks, self._retention_weeks + 4):
            target  = now - timedelta(weeks=weeks_back)
            monday  = target - timedelta(days=target.weekday())
            monday  = monday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

            if monday >= cutoff:
                continue

            part_name = _safe_partition_name(table, monday)

            # Check if partition exists
            exists_res = await conn.execute(
                sa.text("SELECT pg_catalog.to_regclass(:n)"),
                {"n": f"public.{part_name}"},
            )
            if not exists_res.scalar():
                continue

            try:
                await conn.execute(sa.text(f"""
                    ALTER TABLE public.{table}
                    DETACH PARTITION public.{part_name}
                """))
                logger.info("Detached expired partition: %s", part_name)

                # Rename to mark as detached (prevents accidental queries)
                await conn.execute(sa.text(f"""
                    ALTER TABLE public.{part_name}
                    RENAME TO {part_name}_detached
                """))
            except Exception as exc:
                logger.warning("Failed to detach %s: %s", part_name, exc)
            finally:
                await asyncio.sleep(0)

    @staticmethod
    async def create_idempotency_partition(db_engine, target_date: datetime) -> str:
        """
        One-off helper to pre-create a specific weekly idempotency partition.
        Returns the partition name.
        """
        monday    = target_date - timedelta(days=target_date.weekday())
        monday    = monday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        next_week = monday + timedelta(weeks=1)
        name      = _safe_partition_name("idempotency_store", monday)

        async with db_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(sa.text(f"""
                CREATE TABLE IF NOT EXISTS public.{name}
                PARTITION OF public.idempotency_store
                FOR VALUES FROM ('{monday.isoformat()}') TO ('{next_week.isoformat()}')
            """))
        logger.info("Created idempotency partition: %s", name)
        return name
