# redis_utils.py
"""
Async Redis utilities for Doers auth flows.

Features:
- getdel_or_null(key): atomic GET+DEL using GETDEL if available, otherwise Lua fallback.
- get_and_delete_pending_and_email(pending_key, email_key): atomic read+delete both keys (Lua).
- set_json_with_ttl / get_json: store JSON payloads with TTL.
- delete_keys_safe: best-effort delete with logging.
- rate limit helpers: increment_with_ttl, is_rate_limited.
- Uses redis.asyncio.Redis client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional, Sequence

import redis.asyncio as redis

logger = logging.getLogger("doers.redis_utils")
logger.setLevel(logging.INFO)

# Default TTLs (seconds)
DEFAULT_PENDING_TTL = 600  # 10 minutes
DEFAULT_RESEND_RATE_TTL = 3600  # 1 hour

# Lua script to GET and DEL a single key (fallback if GETDEL not available)
_LUA_GETDEL = """
local v = redis.call('GET', KEYS[1])
if v then
  redis.call('DEL', KEYS[1])
end
return v
"""

# Lua script to atomically GET pending_key and DEL pending_key and email_key
# Returns the value of pending_key (or nil)
_LUA_GETDEL_BOTH = """
local v = redis.call('GET', KEYS[1])
if v then
  redis.call('DEL', KEYS[1])
  redis.call('DEL', KEYS[2])
end
return v
"""


class RedisUtils:
    def __init__(self, redis_url: str, *, decode_responses: bool = False):
        """
        Initialize Redis client.

        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379/0)
            decode_responses: whether to decode bytes to str automatically
        """
        self._redis = redis.from_url(redis_url, decode_responses=decode_responses)
        # Preload scripts
        self._getdel_script = None
        self._getdel_both_script = None

    @property
    def client(self) -> redis.Redis:
        return self._redis

    async def close(self) -> None:
        try:
            await self._redis.aclose()
            await self._redis.connection_pool.disconnect()
        except Exception:
            logger.exception("Error closing Redis connection")

    async def _ensure_scripts_loaded(self) -> None:
        if self._getdel_script is None:
            try:
                self._getdel_script = await self._redis.script_load(_LUA_GETDEL)
            except Exception:
                # script_load may fail if Redis unavailable; we'll fallback to EVAL each call
                self._getdel_script = None
        if self._getdel_both_script is None:
            try:
                self._getdel_both_script = await self._redis.script_load(_LUA_GETDEL_BOTH)
            except Exception:
                self._getdel_both_script = None

    async def getdel_or_null(self, key: str) -> Optional[str]:
        """
        Atomically GET and DELETE a single key.
        Guarantees one-time use of tokens.
        """
        try:
            await self._ensure_scripts_loaded()
            if self._getdel_script:
                val = await self._redis.evalsha(self._getdel_script, 1, key)
            else:
                val = await self._redis.eval(_LUA_GETDEL, 1, key)
            
            if val is not None and isinstance(val, bytes):
                return val.decode("utf-8")
            return val
        except Exception:
            logger.exception("getdel_or_null failed for key=%s", key)
            raise

    async def get_and_delete_pending_and_email(self, pending_key: str, email_key: str) -> Optional[str]:
        """
        Atomically GET pending_key and DELETE both pending_key and email_key.
        Returns the pending_key value (string) or None if not present.
        """
        try:
            await self._ensure_scripts_loaded()
            if self._getdel_both_script:
                val = await self._redis.evalsha(self._getdel_both_script, 2, pending_key, email_key)
            else:
                val = await self._redis.eval(_LUA_GETDEL_BOTH, 2, pending_key, email_key)
            return val
        except Exception:
            logger.exception("get_and_delete_pending_and_email failed for keys=%s, %s", pending_key, email_key)
            raise

    async def set_json_with_ttl(self, key: str, value: Any, ttl: int = DEFAULT_PENDING_TTL) -> None:
        """
        Store a JSON-serializable value with TTL (seconds).
        """
        try:
            payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            # Use set with px/EX to ensure TTL is set atomically
            await self._redis.set(key, payload, ex=ttl)
        except Exception:
            logger.exception("Failed to set JSON key=%s", key)
            raise

    async def get_json(self, key: str) -> Optional[Any]:
        """
        Get JSON value and parse it. Returns Python object or None.
        """
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            logger.exception("Failed to get/parse JSON key=%s", key)
            raise

    async def delete_keys_safe(self, keys: Sequence[str]) -> None:
        """
        Best-effort delete multiple keys. Logs failures but does not raise.
        """
        if not keys:
            return
        try:
            await self._redis.delete(*keys)
        except Exception:
            logger.exception("Failed to delete keys=%s", keys)

    # -----------------------
    # Rate limiting helpers
    # -----------------------
    async def increment_with_ttl(self, key: str, ttl: int) -> int:
        """
        Increment a counter and ensure TTL is set.
        Returns the new counter value.
        """
        try:
            # Use Lua to INCR and set TTL only when key is new to avoid race resetting TTL
            lua = """
local v = redis.call('INCR', KEYS[1])
if tonumber(v) == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return v
"""
            val = await self._redis.eval(lua, 1, key, ttl)
            return int(val)
        except Exception:
            logger.exception("increment_with_ttl failed for key=%s", key)
            raise

    async def is_rate_limited(self, key: str, limit: int, ttl: int) -> bool:
        """
        Increment the rate counter and return True if the new count exceeds limit.
        """
        count = await self.increment_with_ttl(key, ttl)
        return count > limit

    # -----------------------
    # Utility helpers
    # -----------------------
    @staticmethod
    def sha256_hex(s: str) -> str:
        import hashlib
        return hashlib.sha256(s.encode("utf-8")).hexdigest()


# -----------------------
# Example usage helpers
# -----------------------
async def example_usage(redis_url: str) -> None:
    """
    Example showing typical usage in signup/verify flows.
    Not executed on import; for developer reference.
    """
    ru = RedisUtils(redis_url)
    try:
        # Example: set pending payload
        token_hash = "signup:pending:abc123"
        email_hash_key = "signup:email:def456"
        payload = {
            "org_name": "Acme Gym",
            "owner_name": "Alice",
            "email": "alice@example.com",
            "hashed_password": "$2b$12$...",
            "facility_type": "gym",
            "resend_count": 0,
            "created_at": "2026-05-13T12:00:00Z",
        }
        await ru.set_json_with_ttl(token_hash, payload, ttl=DEFAULT_PENDING_TTL)
        await ru.client.set(email_hash_key, token_hash, ex=DEFAULT_PENDING_TTL)

        # Atomic read+delete both keys (verify endpoint)
        raw = await ru.get_and_delete_pending_and_email(token_hash, email_hash_key)
        if raw is None:
            logger.info("Token expired or already used")
        else:
            data = json.loads(raw)
            logger.info("Got pending signup: %s", data["email"])

        # Rate limit example
        ip_key = "ratelimit:signup:1.2.3.4"
        is_limited = await ru.is_rate_limited(ip_key, limit=5, ttl=600)
        if is_limited:
            logger.info("IP rate limited")
    finally:
        await ru.close()


# If running as script for quick smoke test (not for production)
if __name__ == "__main__":
    import os
    async def _main():
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        await example_usage(url)
    asyncio.run(_main())
