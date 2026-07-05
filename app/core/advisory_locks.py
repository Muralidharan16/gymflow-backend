"""
app/core/advisory_locks.py
===========================
Distributed lock coordination via PostgreSQL advisory locks.

Design (Master Blueprint Section 7 + patch_v4):
  • DistributedLockCoordinator — dual-integer advisory locking using deterministic
    hash splitting to avoid key-space collisions. UUIDs are split into two int4s.
  • pg_try_advisory_xact_lock() — transaction-scoped (auto-released on commit/rollback).
  • pg_try_advisory_lock() — session-scoped (explicit release required).
  • Lock namespacing prevents collision between different lock domains.

Hash splitting strategy:
    UUID (128 bits) → sha256 → take 64 bits → split into two int32s
    This maps UUIDs into the pg_advisory integer space without collisions.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("doers.advisory_locks")


# ─────────────────────────────────────────────────────────────────────────────
# Lock namespaces — prevent collision between different domains
# ─────────────────────────────────────────────────────────────────────────────

class LockNamespace(IntEnum):
    IDEMPOTENCY    = 0x0001
    KEY_ROTATION   = 0x0002
    PARTITION      = 0x0003
    OUTBOX_WORKER  = 0x0004
    TENANT_QUOTA   = 0x0005
    ADDRESS_UPDATE = 0x0006


# ─────────────────────────────────────────────────────────────────────────────
# UUID → dual-int4 deterministic hash
# ─────────────────────────────────────────────────────────────────────────────

def _uuid_to_dual_int(namespace: LockNamespace, resource_id: str) -> tuple[int, int]:
    """
    Produce a stable pair of signed int32 values from (namespace, resource_id).

    Strategy:
      sha256(namespace:resource_id) → first 8 bytes → two int32s
    The namespace prefix prevents cross-domain hash collisions.
    """
    raw = f"{namespace.value}:{resource_id}".encode()
    digest = hashlib.sha256(raw).digest()
    hi = struct.unpack(">i", digest[:4])[0]   # signed int32
    lo = struct.unpack(">i", digest[4:8])[0]  # signed int32
    return hi, lo


# ─────────────────────────────────────────────────────────────────────────────
# Distributed lock coordinator
# ─────────────────────────────────────────────────────────────────────────────

class DistributedLockCoordinator:
    """
    Acquires PostgreSQL advisory locks using deterministic dual-int4 key hashing.

    Transaction-scoped (default):
        Lock is automatically released when the transaction commits or rolls back.
        Safe for critical sections inside a single request.

    Session-scoped:
        Lock persists until explicitly released or connection closes.
        Use for long-running background workers (e.g. key rotation, partition DDL).
    """

    # ── Transaction-scoped try-lock ────────────────────────────────────────

    @staticmethod
    async def try_lock_transaction(
        db: AsyncSession,
        namespace: LockNamespace,
        resource_id: str,
    ) -> bool:
        """
        Try to acquire a transaction-scoped advisory lock.
        Returns True if acquired, False if contended.
        Auto-released on commit/rollback — no manual release needed.
        """
        hi, lo = _uuid_to_dual_int(namespace, resource_id)
        res = await db.execute(
            sa.text("SELECT pg_catalog.pg_try_advisory_xact_lock(:hi, :lo)"),
            {"hi": hi, "lo": lo},
        )
        acquired = res.scalar()
        if not acquired:
            logger.debug(
                "Advisory lock contended — namespace=%s resource=%s",
                namespace.name, resource_id
            )
        return acquired

    @staticmethod
    @asynccontextmanager
    async def exclusive_transaction_lock(
        db: AsyncSession,
        namespace: LockNamespace,
        resource_id: str,
    ):
        """
        Async context manager for transaction-scoped advisory lock.
        Raises RuntimeError if lock cannot be acquired (contended).
        """
        acquired = await DistributedLockCoordinator.try_lock_transaction(db, namespace, resource_id)
        if not acquired:
            raise RuntimeError(
                f"Could not acquire advisory lock: namespace={namespace.name} resource={resource_id}"
            )
        yield  # lock held for duration of context; released on tx end

    # ── Session-scoped try-lock ────────────────────────────────────────────

    @staticmethod
    async def try_lock_session(
        db: AsyncSession,
        namespace: LockNamespace,
        resource_id: str,
    ) -> bool:
        """
        Try to acquire a session-scoped advisory lock.
        Returns True if acquired. Must call release_session_lock() to release.
        Use for long-running operations that span transaction boundaries.
        """
        hi, lo = _uuid_to_dual_int(namespace, resource_id)
        res = await db.execute(
            sa.text("SELECT pg_catalog.pg_try_advisory_lock(:hi, :lo)"),
            {"hi": hi, "lo": lo},
        )
        return bool(res.scalar())

    @staticmethod
    async def release_session_lock(
        db: AsyncSession,
        namespace: LockNamespace,
        resource_id: str,
    ) -> bool:
        """Release a previously acquired session-scoped advisory lock."""
        hi, lo = _uuid_to_dual_int(namespace, resource_id)
        res = await db.execute(
            sa.text("SELECT pg_catalog.pg_advisory_unlock(:hi, :lo)"),
            {"hi": hi, "lo": lo},
        )
        released = bool(res.scalar())
        if not released:
            logger.warning(
                "pg_advisory_unlock returned false — lock may not have been held: namespace=%s resource=%s",
                namespace.name, resource_id,
            )
        return released

    @staticmethod
    @asynccontextmanager
    async def exclusive_session_lock(
        db: AsyncSession,
        namespace: LockNamespace,
        resource_id: str,
    ):
        """
        Async context manager for session-scoped advisory lock.
        Ensures release even if the body raises.
        """
        acquired = await DistributedLockCoordinator.try_lock_session(db, namespace, resource_id)
        if not acquired:
            raise RuntimeError(
                f"Could not acquire session advisory lock: namespace={namespace.name} resource={resource_id}"
            )
        try:
            yield
        finally:
            await DistributedLockCoordinator.release_session_lock(db, namespace, resource_id)
