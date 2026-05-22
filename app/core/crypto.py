"""
app/core/crypto.py
==================
Enterprise-grade envelope encryption stack for the Doers SaaS platform.

Design decisions captured in blueprint_patch_v4 through v7:
  • BoundedLRUCache    — byte-budgeted, lazy-TTL-evicting DEK cache
  • LockRegistry       — TTL-dict based (NOT WeakValueDictionary) for safe async coordination
  • KMSCircuitBreaker  — async-safe, HALF_OPEN single-probe, per-region via KMSBulkheadRegistry
  • EnvelopeEncryptionProvider — version-aware decrypt, jittered TTL, dual-read migration
  • InstrumentedSemaphore — API-stable telemetry wrapper around asyncio.Semaphore

⚠️  SECURITY THREAT MODEL — CPython Memory Zeroization Limitations
----------------------------------------------------------------------
The bytearray zeroization loops in this module are BEST-EFFORT ONLY.
CPython does NOT guarantee cryptographic-grade secure erasure because:
  • bytes objects created from bytearray are immutable and persist in heap.
  • AESGCM (OpenSSL bindings) copies the key internally.
  • GC sweeps may create additional copies before collection.
For compliance-grade erasure, use HSM / Rust/PyO3 zeroize crate / libsodium.
"""

from __future__ import annotations

import os
import time
import struct
import random
import asyncio
import logging
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from opentelemetry import metrics as otel_metrics
    _meter = otel_metrics.get_meter("doers.kms")
    _OTEL_AVAILABLE = True
except Exception:
    _OTEL_AVAILABLE = False

logger = logging.getLogger("doers.crypto")

_SYSRNG = random.SystemRandom()  # OS-entropy source, immune to PRNG fork-state coupling


# ─────────────────────────────────────────────────────────────────────────────
# 1. Instrumented Semaphore (API-stable telemetry, no CPython internals)
# ─────────────────────────────────────────────────────────────────────────────

class InstrumentedSemaphore:
    """
    asyncio.Semaphore wrapper with explicit, API-stable telemetry.
    Tracks waiting tasks and utilization without accessing private attrs.
    """

    def __init__(self, limit: int, name: str):
        self._sem      = asyncio.Semaphore(limit)
        self._limit    = limit
        self._name     = name
        self._waiting  = 0
        self._acquired = 0
        self._meta_lock = asyncio.Lock()

        if _OTEL_AVAILABLE:
            try:
                _meter.create_observable_gauge(
                    f"io_semaphore_{name}_waiting",
                    callbacks=[lambda _: [otel_metrics.Observation(
                        self._waiting, {"io_class": name}
                    )]],
                    description=f"Tasks waiting for {name} semaphore",
                )
                _meter.create_observable_gauge(
                    f"io_semaphore_{name}_utilization",
                    callbacks=[lambda _: [otel_metrics.Observation(
                        self._acquired / self._limit if self._limit else 0,
                        {"io_class": name},
                    )]],
                    description=f"{name} semaphore utilization ratio (0–1)",
                )
            except Exception:
                pass  # metric registration is non-fatal

    async def __aenter__(self):
        async with self._meta_lock:
            self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            async with self._meta_lock:
                self._waiting  -= 1
                self._acquired += 1
        return self

    async def __aexit__(self, *_):
        async with self._meta_lock:
            self._acquired -= 1
        self._sem.release()


# Module-level I/O guardrails — shared across all providers
class IOGuardrails:
    kms_decrypts: InstrumentedSemaphore
    s3_uploads:   InstrumentedSemaphore

    def __init__(self):
        self.kms_decrypts = InstrumentedSemaphore(50,  "kms")
        self.s3_uploads   = InstrumentedSemaphore(30,  "s3")


_guardrails = IOGuardrails()


# ─────────────────────────────────────────────────────────────────────────────
# 2. TTL-based Lock Registry (replaces WeakValueDictionary)
# ─────────────────────────────────────────────────────────────────────────────

class LockRegistry:
    """
    Stores asyncio.Lock objects keyed by string with a last-access timestamp.
    Stale, unlocked entries are evicted by `sweep_stale()` (called from supervisor).
    Never uses WeakValueDictionary — GC-coupling of coordination primitives is unsafe.
    """
    _TTL = 600.0  # seconds

    def __init__(self):
        self._locks: Dict[str, Tuple[asyncio.Lock, float]] = {}
        self._guard = asyncio.Lock()

    async def get_or_create(self, key: str) -> asyncio.Lock:
        async with self._guard:
            entry = self._locks.get(key)
            if entry:
                lock, _ = entry
                self._locks[key] = (lock, time.monotonic())
                return lock
            lock = asyncio.Lock()
            self._locks[key] = (lock, time.monotonic())
            return lock

    async def sweep_stale(self) -> int:
        now = time.monotonic()
        async with self._guard:
            stale = [
                k for k, (lk, ts) in self._locks.items()
                if not lk.locked() and now - ts > self._TTL
            ]
            for k in stale:
                del self._locks[k]
        return len(stale)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Byte-Budgeted Bounded LRU Cache with Lazy TTL Eviction
# ─────────────────────────────────────────────────────────────────────────────

class BoundedLRUCache:
    """
    Thread-safe Bounded LRU Cache for DEKs, budgeted by byte size.
    Expired entries are evicted lazily on access (get/set).
    """

    def __init__(self, max_memory_bytes: int = 10 * 1024 * 1024):
        self.max_memory_bytes    = max_memory_bytes
        self.current_memory_bytes = 0
        self.cache: OrderedDict[str, Tuple[bytearray, float]] = OrderedDict()
        self.lock  = asyncio.Lock()

    def _entry_size(self, key: str, val: bytearray) -> int:
        return len(key.encode()) + len(val) + 64

    async def get(self, key: str) -> Optional[Tuple[bytearray, float]]:
        async with self.lock:
            entry = self.cache.get(key)
            if entry is None:
                return None
            val, expire_at = entry
            if time.time() >= expire_at:
                self.current_memory_bytes -= self._entry_size(key, val)
                del self.cache[key]
                for i in range(len(val)):
                    val[i] = 0
                return None
            self.cache.move_to_end(key)
            return entry

    async def set(self, key: str, value: Tuple[bytearray, float]):
        async with self.lock:
            val, expire_at = value
            new_size = self._entry_size(key, val)

            if key in self.cache:
                old_val, _ = self.cache[key]
                self.current_memory_bytes -= self._entry_size(key, old_val)
                for i in range(len(old_val)):
                    old_val[i] = 0
                self.cache.move_to_end(key)

            while self.current_memory_bytes + new_size > self.max_memory_bytes and self.cache:
                oldest_key, (oldest_val, _) = self.cache.popitem(last=False)
                self.current_memory_bytes -= self._entry_size(oldest_key, oldest_val)
                for i in range(len(oldest_val)):
                    oldest_val[i] = 0

            self.cache[key] = value
            self.current_memory_bytes += new_size


# ─────────────────────────────────────────────────────────────────────────────
# 4. KMS Circuit Breaker — async-safe, HALF_OPEN single probe, observable
# ─────────────────────────────────────────────────────────────────────────────

_STATE_CODE = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}

# Module-level breaker state dict for observable gauge callback
_breaker_states: Dict[str, int] = {}

if _OTEL_AVAILABLE:
    try:
        _breaker_open_counter  = _meter.create_counter("kms_circuit_breaker_open_total")
        _probe_success_counter = _meter.create_counter("kms_circuit_breaker_probe_success_total")
        _probe_failure_counter = _meter.create_counter("kms_circuit_breaker_probe_failure_total")
        _rejection_counter     = _meter.create_counter("kms_circuit_breaker_rejected_total")

        def _breaker_state_cb(options):
            for name, code in _breaker_states.items():
                yield otel_metrics.Observation(code, {"kms_provider": name})

        _meter.create_observable_gauge(
            "kms_circuit_breaker_state",
            callbacks=[_breaker_state_cb],
            description="0=CLOSED 1=HALF_OPEN 2=OPEN",
        )
    except Exception:
        pass


class KMSCircuitBreakerError(Exception):
    pass


class KMSCircuitBreaker:
    def __init__(self, provider_name: str, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.provider_name    = provider_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.state             = "CLOSED"
        self.failure_count     = 0
        self.last_failure_time = 0.0
        self._lock             = asyncio.Lock()
        self._probe_inflight   = False
        _breaker_states[provider_name] = _STATE_CODE["CLOSED"]

    async def record_success(self):
        async with self._lock:
            prev = self.state
            self.failure_count   = 0
            self.state           = "CLOSED"
            self._probe_inflight = False
        _breaker_states[self.provider_name] = _STATE_CODE["CLOSED"]
        if prev == "HALF_OPEN" and _OTEL_AVAILABLE:
            try:
                _probe_success_counter.add(1, {"provider": self.provider_name})
            except Exception:
                pass
        logger.debug("KMS breaker CLOSED for %s", self.provider_name)

    async def record_failure(self):
        async with self._lock:
            self.failure_count    += 1
            self.last_failure_time = time.time()
            self._probe_inflight   = False
            if self.failure_count >= self.failure_threshold:
                if self.state != "OPEN":
                    logger.critical("KMS breaker OPEN for %s after %d failures", self.provider_name, self.failure_count)
                    if _OTEL_AVAILABLE:
                        try:
                            _breaker_open_counter.add(1, {"provider": self.provider_name})
                        except Exception:
                            pass
                self.state = "OPEN"
            if self.state == "HALF_OPEN" and _OTEL_AVAILABLE:
                try:
                    _probe_failure_counter.add(1, {"provider": self.provider_name})
                except Exception:
                    pass
        _breaker_states[self.provider_name] = _STATE_CODE[self.state]

    async def allow_request(self) -> bool:
        async with self._lock:
            if self.state == "CLOSED":
                return True
            if self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    if not self._probe_inflight:
                        self.state           = "HALF_OPEN"
                        self._probe_inflight = True
                        _breaker_states[self.provider_name] = _STATE_CODE["HALF_OPEN"]
                        logger.info("KMS breaker HALF_OPEN probe inflight for %s", self.provider_name)
                        return True
                if _OTEL_AVAILABLE:
                    try:
                        _rejection_counter.add(1, {"provider": self.provider_name})
                    except Exception:
                        pass
                return False
            return False  # HALF_OPEN, probe already inflight


# ─────────────────────────────────────────────────────────────────────────────
# 5. Per-Region KMS Bulkhead Registry with TTL eviction
# ─────────────────────────────────────────────────────────────────────────────

class KMSBulkheadRegistry:
    _IDLE_TTL = 3600.0

    def __init__(self):
        self._entries: Dict[str, Tuple[KMSCircuitBreaker, float]] = {}
        self._lock    = asyncio.Lock()

    async def get_breaker(self, region: str, account: str = "default") -> KMSCircuitBreaker:
        key = f"{region}:{account}"
        async with self._lock:
            entry = self._entries.get(key)
            if entry:
                breaker, _ = entry
                self._entries[key] = (breaker, time.monotonic())
                return breaker
            breaker = KMSCircuitBreaker(provider_name=key)
            self._entries[key] = (breaker, time.monotonic())
            return breaker

    async def sweep_idle_breakers(self) -> int:
        now = time.monotonic()
        async with self._lock:
            stale = [
                k for k, (b, ts) in self._entries.items()
                if b.state == "CLOSED" and now - ts > self._IDLE_TTL
            ]
            for k in stale:
                del self._entries[k]
        if stale:
            logger.info("KMS bulkhead registry: evicted %d idle breakers.", len(stale))
        return len(stale)


# Singleton registry
kms_bulkhead = KMSBulkheadRegistry()


# ─────────────────────────────────────────────────────────────────────────────
# 6. KMS Provider (wraps raw master key; pluggable for real KMS)
# ─────────────────────────────────────────────────────────────────────────────

class KMSProvider:
    """
    Wraps a raw master key and decrypts DEK envelopes.
    In production, replace _do_decrypt with AWS KMS / GCP KMS SDK calls.
    Each decrypt is gated by a per-region circuit breaker and the kms semaphore.
    """

    def __init__(self, raw_master_key: bytes, region: str = "us-east-1", account: str = "default"):
        self._key    = raw_master_key
        self._region = region
        self._account = account

    async def decrypt_dek(self, encrypted_dek: bytes) -> bytes:
        breaker = await kms_bulkhead.get_breaker(self._region, self._account)
        if not await breaker.allow_request():
            raise KMSCircuitBreakerError(
                f"KMS breaker OPEN for region={self._region}. Rejecting decrypt."
            )
        async with _guardrails.kms_decrypts:
            try:
                result = self._do_decrypt(encrypted_dek)
                await breaker.record_success()
                return result
            except KMSCircuitBreakerError:
                raise
            except Exception as exc:
                await breaker.record_failure()
                raise RuntimeError(f"KMS decrypt failed: {exc}") from exc

    def _do_decrypt(self, encrypted_dek: bytes) -> bytes:
        """AES-GCM unwrap of the DEK envelope (nonce prepended)."""
        aesgcm = AESGCM(self._key)
        nonce      = encrypted_dek[:12]
        ciphertext = encrypted_dek[12:]
        return aesgcm.decrypt(nonce, ciphertext, None)

    def encrypt_dek(self, raw_dek: bytes) -> bytes:
        """AES-GCM wrap of a raw DEK (nonce prepended)."""
        aesgcm = AESGCM(self._key)
        nonce = os.urandom(12)
        return nonce + aesgcm.encrypt(nonce, raw_dek, None)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Envelope Encryption Provider — version-aware, jittered TTL, dual-read
# ─────────────────────────────────────────────────────────────────────────────

class EnvelopeEncryptionProvider:
    """
    Enterprise envelope encryption with:
      • Version-aware DEK resolution for dual-read after rotation.
      • Byte-budgeted LRU DEK cache with lazy TTL eviction.
      • Jittered TTL (±10%) to prevent synchronized KMS stampedes.
      • Per-key asyncio.Lock via TTL-safe LockRegistry.
      • AES-256-GCM with unique nonce + AAD binding per record.

    ⚠️  Memory zeroization is best-effort only under CPython. See module docstring.
    """

    _dek_cache    = BoundedLRUCache(max_memory_bytes=10 * 1024 * 1024)
    _lock_registry = LockRegistry()

    # DEK registry lookup fn — injected at startup (avoids circular imports)
    # Signature: async (tenant_id: str, key_version: int) -> bytes
    _dek_registry_fn = None

    @classmethod
    def register_dek_lookup(cls, fn) -> None:
        """
        Register the async callable used to fetch encrypted DEKs for historical versions.
        Must be called during application startup before any decrypt() calls.

        fn signature: async (tenant_id: str, key_version: int) -> bytes
        """
        cls._dek_registry_fn = fn

    def __init__(self, tenant_id: str, encrypted_dek: bytes, kms_provider: KMSProvider, key_version: int):
        self.tenant_id     = tenant_id
        self.encrypted_dek = encrypted_dek
        self.kms_provider  = kms_provider
        self.key_version   = key_version

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _get_decrypted_dek(self, version: Optional[int] = None) -> bytearray:
        """Fetch and cache a DEK by version. Falls back to active version if unspecified."""
        ver       = version if version is not None else self.key_version
        cache_key = f"{self.tenant_id}:{ver}"
        lock      = await self._lock_registry.get_or_create(cache_key)

        async with lock:
            cached = await self._dek_cache.get(cache_key)
            if cached:
                dek, _ = cached
                return bytearray(dek)

            # Fetch the correct encrypted DEK for this version
            if ver != self.key_version and self._dek_registry_fn is not None:
                enc_dek = await self._dek_registry_fn(self.tenant_id, ver)
            else:
                enc_dek = self.encrypted_dek

            try:
                decrypted = await self.kms_provider.decrypt_dek(enc_dek)
            except KMSCircuitBreakerError:
                # Attempt stale cache fallback
                stale = await self._dek_cache.get(cache_key)
                if stale:
                    logger.warning("KMS down; using stale DEK cache for tenant=%s ver=%d", self.tenant_id, ver)
                    return bytearray(stale[0])
                raise

            mutable_dek = bytearray(decrypted)

            # Jittered TTL: ±10% around 300s prevents synchronized expiry waves
            BASE_TTL = 300.0
            jitter   = _SYSRNG.uniform(-30.0, 30.0)
            expire_at = time.time() + BASE_TTL + jitter

            await self._dek_cache.set(cache_key, (bytearray(mutable_dek), expire_at))
            return mutable_dek

    # ── Public API ─────────────────────────────────────────────────────────

    async def encrypt(self, plaintext: str, record_id: str, version: int) -> Tuple[bytes, int]:
        """Encrypt plaintext and return (ciphertext_bytes, key_version)."""
        if not plaintext:
            return b"", self.key_version

        raw_dek    = await self._get_decrypted_dek()
        dek_buffer = bytearray(raw_dek)
        try:
            aesgcm = AESGCM(bytes(dek_buffer))
            nonce  = os.urandom(12)
            aad    = f"v1:{self.tenant_id}:{record_id}:{version}".encode()
            encrypted = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
            header    = struct.pack(">I", self.key_version)
            return header + nonce + encrypted, self.key_version
        finally:
            for i in range(len(dek_buffer)):
                dek_buffer[i] = 0

    async def decrypt(self, ciphertext_bytes: bytes, record_id: str, version: int) -> str:
        """
        Decrypt ciphertext using the version embedded in the envelope header.
        Supports dual-read after key rotation (reads with any registered version).
        """
        if not ciphertext_bytes:
            return ""

        # Parse embedded key version from header
        embedded_ver = struct.unpack(">I", ciphertext_bytes[:4])[0]
        if embedded_ver != self.key_version:
            logger.info(
                "Dual-read: decrypting with legacy key version %d for tenant=%s",
                embedded_ver, self.tenant_id
            )

        raw_dek    = await self._get_decrypted_dek(version=embedded_ver)
        dek_buffer = bytearray(raw_dek)
        try:
            aesgcm    = AESGCM(bytes(dek_buffer))
            nonce     = ciphertext_bytes[4:16]
            encrypted = ciphertext_bytes[16:]
            aad       = f"v1:{self.tenant_id}:{record_id}:{version}".encode()
            return aesgcm.decrypt(nonce, encrypted, aad).decode("utf-8")
        finally:
            for i in range(len(dek_buffer)):
                dek_buffer[i] = 0
