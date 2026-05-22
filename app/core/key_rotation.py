"""
app/core/key_rotation.py
=========================
Phased, resumable DEK (Data Encryption Key) rotation orchestrator.

Design (Master Blueprint Section 9 + patch_v4/v5/v6):
  • Phase 1: Generate new DEK, encrypt with KMS master key, persist to key_registry.
             Set key status = 'ACTIVE', old key = 'DEPRECATED'.
  • Phase 2: Async consumers start using new active DEK for all new encryptions.
             Old DEK remains readable via dual-read (version header in ciphertext).
  • Phase 3: Resumable batch sweep — re-encrypts all rows encrypted with the old DEK.
             Each batch is its own short transaction (per-batch BEGIN/COMMIT).
             Watermark persisted in key_rotation_progress for crash safety.
  • Phase 4: Old DEK marked 'RETIRED'. Dual-read fallback removed after grace period.

Critical correctness properties:
  • Per-batch asyncio.timeout(30s) prevents xmin freeze under autovacuum.
  • Session advisory lock (KEY_ROTATION namespace) prevents concurrent sweeps.
  • ContextVar retry guard prevents nested retry amplification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_locks import DistributedLockCoordinator, LockNamespace
from app.core.concurrency import retry_on_transaction_failure
from app.core.crypto import KMSProvider, kms_bulkhead

logger = logging.getLogger("doers.key_rotation")

_BATCH_SIZE    = 500
_BATCH_TIMEOUT = 30    # seconds — each batch must complete within 30s
_MAX_RUNTIME   = 3600  # seconds — total sweep runtime ceiling


# ─────────────────────────────────────────────────────────────────────────────
# Key rotation orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class PhasedKeyRotationOrchestrator:
    """
    Manages the four-phase lifecycle of DEK rotation for a single tenant.
    Designed to run as a supervised background worker or a one-off admin task.
    """

    def __init__(self, kms_provider: KMSProvider, db_factory):
        """
        Args:
            kms_provider: KMS instance for this tenant/region.
            db_factory:   Async callable returning an AsyncSession context manager.
        """
        self._kms        = kms_provider
        self._db_factory = db_factory

    # ── Phase 1: Generate + Persist new DEK ───────────────────────────────

    async def phase1_generate_new_dek(
        self,
        db: AsyncSession,
        tenant_id: str,
        table_name: str,
    ) -> int:
        """
        Generates a new 256-bit DEK, wraps it with KMS, and inserts it into
        public.encryption_key_registry. Returns the new key_version.
        """
        raw_dek       = os.urandom(32)
        encrypted_dek = self._kms.encrypt_dek(raw_dek)

        # Deprecate existing active key for this tenant/table
        await db.execute(
            sa.text("""
                UPDATE public.encryption_key_registry
                SET key_status = 'DEPRECATED', deprecated_at = :now
                WHERE tenant_id = :tid AND table_name = :tbl AND key_status = 'ACTIVE'
            """),
            {"tid": tenant_id, "tbl": table_name, "now": datetime.now(timezone.utc)},
        )

        # Insert new active key
        res = await db.execute(
            sa.text("""
                INSERT INTO public.encryption_key_registry
                    (tenant_id, table_name, encrypted_dek, key_status, created_at)
                VALUES (:tid, :tbl, :enc, 'ACTIVE', :now)
                RETURNING key_version
            """),
            {
                "tid": tenant_id,
                "tbl": table_name,
                "enc": encrypted_dek,
                "now": datetime.now(timezone.utc),
            },
        )
        new_version = res.scalar()
        await db.commit()
        logger.info("Phase 1 complete: tenant=%s table=%s new_version=%d", tenant_id, table_name, new_version)
        return new_version

    # ── Phase 3: Resumable batch re-encryption sweep ──────────────────────

    async def phase3_resumable_sweep(
        self,
        tenant_id: str,
        table_name: str,
        old_version: int,
        new_version: int,
    ):
        """
        Re-encrypts all rows still encrypted with old_version DEK.
        Resumable: watermark persisted in key_rotation_progress.
        Bounded: per-batch 30s timeout, total 1h ceiling.
        Advisory lock: prevents concurrent sweeps for the same tenant/table.
        """
        async with self._db_factory() as db:
            # Acquire session-scoped advisory lock — prevents concurrent rotations
            lock_key = f"{tenant_id}:{table_name}"
            acquired = await DistributedLockCoordinator.try_lock_session(
                db, LockNamespace.KEY_ROTATION, lock_key
            )
            if not acquired:
                logger.warning("Rotation sweep already running for %s/%s. Skipping.", tenant_id, table_name)
                return

            try:
                await self._sweep_loop(db, tenant_id, table_name, old_version, new_version)
            finally:
                await DistributedLockCoordinator.release_session_lock(
                    db, LockNamespace.KEY_ROTATION, lock_key
                )

    async def _sweep_loop(
        self,
        db: AsyncSession,
        tenant_id: str,
        table_name: str,
        old_version: int,
        new_version: int,
    ):
        watermark = await self._fetch_watermark(db, tenant_id, table_name)

        try:
            async with asyncio.timeout(_MAX_RUNTIME):
                while True:
                    await asyncio.sleep(0)  # cancellation checkpoint

                    try:
                        async with asyncio.timeout(_BATCH_TIMEOUT):
                            processed = await self._process_batch(
                                db, tenant_id, table_name, old_version, new_version, watermark
                            )
                    except asyncio.TimeoutError:
                        logger.error(
                            "Batch timeout (>%ds) during rotation sweep tenant=%s. Watermark preserved.",
                            _BATCH_TIMEOUT, tenant_id
                        )
                        return

                    if processed == 0:
                        logger.info(
                            "Phase 3 sweep complete: tenant=%s table=%s old_v=%d new_v=%d",
                            tenant_id, table_name, old_version, new_version
                        )
                        await self._mark_old_key_retired(db, tenant_id, table_name, old_version)
                        return

                    watermark = await self._fetch_watermark(db, tenant_id, table_name)

        except asyncio.TimeoutError:
            logger.warning("Rotation sweep hit total timeout (%ds). Watermark at %s.", _MAX_RUNTIME, watermark)
        except asyncio.CancelledError:
            logger.info("Rotation sweep cancelled. Watermark preserved at %s.", watermark)
            raise

    @retry_on_transaction_failure(max_retries=3, db_only=True)
    async def _process_batch(
        self,
        db: AsyncSession,
        tenant_id: str,
        table_name: str,
        old_version: int,
        new_version: int,
        watermark: Optional[str],
    ) -> int:
        """
        Fetch one batch of rows with old_version, re-encrypt them, commit.
        Returns number of rows processed (0 = sweep complete).
        Each call is its own transaction — keeps xmin advancing for autovacuum.
        """
        # Fetch rows encrypted with old_version (beyond watermark)
        res = await db.execute(
            sa.text("""
                SELECT id, payload_encrypted, key_version
                FROM public.organization_address_payloads_secure
                WHERE tenant_id = :tid
                  AND key_version = :old_v
                  AND (:wm IS NULL OR id > :wm::uuid)
                ORDER BY id
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            """),
            {"tid": tenant_id, "old_v": old_version, "wm": watermark, "batch": _BATCH_SIZE},
        )
        rows = res.fetchall()
        if not rows:
            return 0

        # Fetch DEKs for both versions
        old_raw = await self._kms.decrypt_dek(
            await self._get_encrypted_dek(db, tenant_id, old_version)
        )
        new_raw = await self._kms.decrypt_dek(
            await self._get_encrypted_dek(db, tenant_id, new_version)
        )

        old_dek = bytearray(old_raw)
        new_dek = bytearray(new_raw)

        try:
            for row in rows:
                await asyncio.sleep(0)  # inner cancellation checkpoint
                rewrapped = self._rewrap(bytes(old_dek), bytes(new_dek), row.payload_encrypted, new_version)
                await db.execute(
                    sa.text("""
                        UPDATE public.organization_address_payloads_secure
                        SET payload_encrypted = :payload, key_version = :new_v
                        WHERE id = :row_id AND tenant_id = :tid
                    """),
                    {"payload": rewrapped, "new_v": new_version, "row_id": row.id, "tid": tenant_id},
                )

            # Commit this batch and advance watermark — xmin advances here
            last_id = str(rows[-1].id)
            await db.execute(
                sa.text("""
                    INSERT INTO public.key_rotation_progress (tenant_id, table_name, last_processed_pk, updated_at)
                    VALUES (:tid, :tbl, :pk::uuid, :now)
                    ON CONFLICT (tenant_id, table_name) DO UPDATE
                    SET last_processed_pk = :pk::uuid, updated_at = :now
                """),
                {"tid": tenant_id, "tbl": table_name, "pk": last_id, "now": datetime.now(timezone.utc)},
            )
            await db.commit()
            logger.debug("Rotation batch: %d rows, watermark=%s", len(rows), last_id)
            return len(rows)

        finally:
            for i in range(len(old_dek)):
                old_dek[i] = 0
            for i in range(len(new_dek)):
                new_dek[i] = 0

    def _rewrap(self, old_dek: bytes, new_dek: bytes, ciphertext: bytes, new_version: int) -> bytes:
        """Decrypt with old DEK, re-encrypt with new DEK, update version header."""
        old_aesgcm = AESGCM(old_dek)
        new_aesgcm = AESGCM(new_dek)
        nonce     = ciphertext[4:16]
        payload   = ciphertext[16:]
        # Decrypt (strips old header)
        plaintext = old_aesgcm.decrypt(nonce, payload, None)
        # Re-encrypt with new DEK + new nonce
        new_nonce     = os.urandom(12)
        new_header    = struct.pack(">I", new_version)
        new_ciphertext = new_aesgcm.encrypt(new_nonce, plaintext, None)
        return new_header + new_nonce + new_ciphertext

    async def _get_encrypted_dek(self, db: AsyncSession, tenant_id: str, version: int) -> bytes:
        res = await db.execute(
            sa.text("""
                SELECT encrypted_dek FROM public.encryption_key_registry
                WHERE tenant_id = :tid AND key_version = :ver
            """),
            {"tid": tenant_id, "ver": version},
        )
        row = res.fetchone()
        if not row:
            raise ValueError(f"DEK not found: tenant={tenant_id} version={version}")
        return bytes(row.encrypted_dek)

    async def _fetch_watermark(self, db: AsyncSession, tenant_id: str, table_name: str) -> Optional[str]:
        res = await db.execute(
            sa.text("""
                SELECT last_processed_pk::text FROM public.key_rotation_progress
                WHERE tenant_id = :tid AND table_name = :tbl
            """),
            {"tid": tenant_id, "tbl": table_name},
        )
        row = res.fetchone()
        return row.last_processed_pk if row else None

    async def _mark_old_key_retired(
        self, db: AsyncSession, tenant_id: str, table_name: str, old_version: int
    ):
        await db.execute(
            sa.text("""
                UPDATE public.encryption_key_registry
                SET key_status = 'RETIRED', retired_at = :now
                WHERE tenant_id = :tid AND table_name = :tbl AND key_version = :ver
            """),
            {"tid": tenant_id, "tbl": table_name, "ver": old_version, "now": datetime.now(timezone.utc)},
        )
        await db.commit()
        logger.info("Phase 4: DEK version %d retired for tenant=%s", old_version, tenant_id)
