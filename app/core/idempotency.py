"""
app/core/idempotency.py
========================
Enterprise idempotency engine for the Doers SaaS platform.

Architecture (blueprint_patch_v5 + v7):
  • active_idempotency_keys  — small non-partitioned anchor table for uniqueness enforcement.
    PostgreSQL CANNOT enforce global uniqueness across RANGE partitions; this table is the
    uniqueness boundary. Waiters poll here, not the heavy partitioned payload table.
  • idempotency_store        — RANGE-partitioned (weekly) for payload storage and archival.
  • Heartbeat column + zombie reclaim sweep (v7 FIX 14).
  • HMAC verification on replay (v7 FIX 15).
  • Exponential backoff waiter with absolute timeout (no infinite spins).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import random
import time
import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("doers.idempotency")


class IdempotencyError(Exception):
    pass


class IdempotencyTamperedError(IdempotencyError):
    pass


class IdempotencyTimeoutError(IdempotencyError):
    pass


class IdempotencyEngine:
    """
    Transactionally isolated request idempotency using a two-table architecture:

      active_idempotency_keys  ← small, hot, uniqueness-enforced (PRIMARY KEY)
      idempotency_store        ← partitioned, holds full payload + metadata

    Flow:
      1. Atomically INSERT INTO active_idempotency_keys ON CONFLICT DO NOTHING.
      2. Winner: also inserts payload stub into idempotency_store; executes request.
      3. Winner: calls complete_idempotency() to store result.
      4. Loser waiters: poll active_idempotency_keys.status with exponential backoff.
      5. On replay: verify HMAC then return cached response.
    """

    _INLINE_LIMIT = 8 * 1024  # 8 KB inline; larger goes to S3

    def __init__(self, secret_key: str):
        self._secret = secret_key.encode()

    # ── Hash helpers ───────────────────────────────────────────────────────

    def canonical_hash(self, payload: dict) -> str:
        """Deterministic SHA-256 of the request payload (sorted keys)."""
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    def response_hmac(self, response_bytes: bytes) -> str:
        return _hmac.new(self._secret, response_bytes, hashlib.sha256).hexdigest()

    def verify_hmac(self, response_bytes: bytes, stored_hmac: str) -> None:
        """Raise IdempotencyTamperedError if HMAC does not match."""
        expected = self.response_hmac(response_bytes)
        if not _hmac.compare_digest(expected, stored_hmac):
            raise IdempotencyTamperedError("Idempotency response HMAC mismatch — possible tampering detected.")

    # ── Lock acquisition ───────────────────────────────────────────────────

    async def acquire(
        self,
        db: AsyncSession,
        tenant_id: str,
        ikey: str,
        request_hash: str,
        worker_id: Optional[uuid.UUID] = None,
        max_wait_sec: int = 15,
    ) -> str:
        """
        Returns:
          "WINNER"    — this request won the lock; caller must execute and complete.
          "COMPLETED" — a previous identical request succeeded; caller should replay.
          "FAILED"    — a previous attempt failed; lock was reclaimed.

        Raises:
          IdempotencyTimeoutError — winner did not complete within max_wait_sec.
        """
        wid = worker_id or uuid.uuid4()
        partition = _current_partition_name()

        # 1. Try to atomically claim the key in the non-partitioned anchor table
        res = await db.execute(
            sa.text("""
                INSERT INTO public.active_idempotency_keys
                    (tenant_id, idempotency_key, status, heartbeat_at,
                     owner_worker_id, partition_name)
                VALUES (:tid, :ikey, 'IN_PROGRESS',
                        pg_catalog.clock_timestamp(), :wid, :part)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING status
            """),
            {"tid": tenant_id, "ikey": ikey, "wid": str(wid), "part": partition},
        )
        row = res.fetchone()

        if row:
            # Won — write payload stub to partitioned store
            await db.execute(
                sa.text("""
                    INSERT INTO public.idempotency_store
                        (tenant_id, idempotency_key, request_hash, status, created_at)
                    VALUES (:tid, :ikey, :rhash, 'IN_PROGRESS', pg_catalog.clock_timestamp())
                    ON CONFLICT (tenant_id, idempotency_key, created_at) DO NOTHING
                """),
                {"tid": tenant_id, "ikey": ikey, "rhash": request_hash},
            )
            await db.commit()
            return "WINNER"

        # Lost — poll anchor table for completion
        return await self._poll_for_completion(db, tenant_id, ikey, max_wait_sec)

    async def _poll_for_completion(
        self, db: AsyncSession, tenant_id: str, ikey: str, max_wait_sec: int
    ) -> str:
        start_time = time.monotonic()
        delay      = 0.1
        while time.monotonic() - start_time < max_wait_sec:
            res = await db.execute(
                sa.text("""
                    SELECT status FROM public.active_idempotency_keys
                    WHERE tenant_id = :tid AND idempotency_key = :ikey
                """),
                {"tid": tenant_id, "ikey": ikey},
            )
            row = res.fetchone()
            if row:
                if row.status == "COMPLETED":
                    return "COMPLETED"
                if row.status == "FAILED":
                    return "FAILED"
            await asyncio.sleep(delay + random.uniform(0.01, 0.05))
            delay = min(2.0, delay * 1.5)

        raise IdempotencyTimeoutError(
            f"Idempotency winner did not complete within {max_wait_sec}s. Key={ikey}"
        )

    # ── Heartbeat renewal ──────────────────────────────────────────────────

    async def renew_heartbeat(self, db: AsyncSession, tenant_id: str, ikey: str, worker_id: uuid.UUID):
        """Winner calls this periodically (~every 5s) to prove it is alive."""
        await db.execute(
            sa.text("""
                UPDATE public.active_idempotency_keys
                SET heartbeat_at = pg_catalog.clock_timestamp()
                WHERE tenant_id = :tid AND idempotency_key = :ikey
                  AND owner_worker_id = :wid AND status = 'IN_PROGRESS'
            """),
            {"tid": tenant_id, "ikey": ikey, "wid": str(worker_id)},
        )
        await db.commit()

    # ── Completion ────────────────────────────────────────────────────────

    async def complete(
        self,
        db: AsyncSession,
        tenant_id: str,
        ikey: str,
        status_code: int,
        response_payload: dict,
    ):
        payload_bytes = json.dumps(response_payload, sort_keys=True).encode()
        hmac_hex      = self.response_hmac(payload_bytes)

        stored_payload: Optional[bytes] = payload_bytes if len(payload_bytes) <= self._INLINE_LIMIT else None

        await db.execute(
            sa.text("""
                UPDATE public.idempotency_store
                SET status           = 'COMPLETED',
                    response_code    = :code,
                    response_payload = :payload,
                    response_hmac    = :hmac
                WHERE tenant_id = :tid AND idempotency_key = :ikey
                  AND status = 'IN_PROGRESS'
            """),
            {
                "tid": tenant_id, "ikey": ikey,
                "code": status_code,
                "payload": stored_payload,
                "hmac": hmac_hex,
            },
        )
        await db.execute(
            sa.text("""
                UPDATE public.active_idempotency_keys
                SET status = 'COMPLETED'
                WHERE tenant_id = :tid AND idempotency_key = :ikey
            """),
            {"tid": tenant_id, "ikey": ikey},
        )
        await db.commit()

    # ── Replay retrieval ──────────────────────────────────────────────────

    async def get_completed(
        self, db: AsyncSession, tenant_id: str, ikey: str
    ) -> Optional[dict]:
        """
        Returns cached response dict if a completed record exists.
        Verifies HMAC before returning — raises IdempotencyTamperedError on mismatch.
        """
        res = await db.execute(
            sa.text("""
                SELECT s.response_code, s.response_payload, s.response_hmac
                FROM public.idempotency_store s
                JOIN public.active_idempotency_keys k
                  ON k.tenant_id = s.tenant_id AND k.idempotency_key = s.idempotency_key
                WHERE s.tenant_id = :tid AND s.idempotency_key = :ikey
                  AND k.status = 'COMPLETED'
                LIMIT 1
            """),
            {"tid": tenant_id, "ikey": ikey},
        )
        row = res.fetchone()
        if not row or not row.response_payload:
            return None

        self.verify_hmac(row.response_payload, row.response_hmac)
        return {
            "status_code": row.response_code,
            "body": json.loads(row.response_payload),
        }

    # ── Zombie reclaim sweep ──────────────────────────────────────────────

    @staticmethod
    async def reclaim_zombies(db: AsyncSession, stale_threshold_sec: int = 30):
        """
        Background sweep — reclaims IN_PROGRESS keys whose winner heartbeat
        has not been renewed within stale_threshold_sec seconds.
        Run from supervisor every 60s.
        """
        res = await db.execute(
            sa.text("""
                UPDATE public.active_idempotency_keys
                SET status = 'FAILED'
                WHERE status = 'IN_PROGRESS'
                  AND heartbeat_at < pg_catalog.clock_timestamp()
                                    - (:t * interval '1 second')
                RETURNING tenant_id, idempotency_key
            """),
            {"t": stale_threshold_sec},
        )
        rows = res.fetchall()
        if rows:
            logger.warning("Reclaimed %d zombie idempotency locks.", len(rows))
        await db.commit()

    @staticmethod
    async def archive_expired_anchor_keys(db: AsyncSession, retention_hours: int = 48):
        """Removes COMPLETED entries older than retention_hours from the anchor table."""
        await db.execute(
            sa.text("""
                DELETE FROM public.active_idempotency_keys
                WHERE status = 'COMPLETED'
                  AND created_at < pg_catalog.clock_timestamp()
                                  - (:h * interval '1 hour')
            """),
            {"h": retention_hours},
        )
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Partition name helper
# ─────────────────────────────────────────────────────────────────────────────

import datetime, re

_PARTITION_RE = re.compile(r"^idempotency_store_y\d{4}_m\d{2}_d\d{2}$")


def _current_partition_name() -> str:
    now  = datetime.datetime.now(datetime.timezone.utc)
    name = f"idempotency_store_y{now.year}_m{now.month:02d}_d{now.day:02d}"
    if not _PARTITION_RE.match(name):
        raise ValueError(f"Unsafe partition name generated: {name}")
    return name
