# app/core/redis.py
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.core.redis_utils import RedisUtils

logger = logging.getLogger(__name__)

# Internal instance
_redis_utils: Optional[RedisUtils] = None


async def init_redis() -> None:
    """
    Initialize the RedisUtils instance and preload scripts.
    Call this on FastAPI startup.
    """
    global _redis_utils
    if _redis_utils is None:
        _redis_utils = RedisUtils(settings.REDIS_URL, decode_responses=True)
        try:
            await _redis_utils._ensure_scripts_loaded()
        except Exception:
            # If script preload fails, keep the client — scripts will be eval'd on first use.
            logger.exception("Failed to preload Redis Lua scripts; continuing.")


async def close_redis() -> None:
    global _redis_utils
    if _redis_utils is not None:
        try:
            await _redis_utils.close()
        except Exception:
            logger.exception("Error closing RedisUtils")
        _redis_utils = None


def get_redis_utils() -> RedisUtils:
    assert _redis_utils is not None, "RedisUtils not initialized; call init_redis() on startup"
    return _redis_utils


# -----------------------
# Backwards-compatible Resilient wrapper
# -----------------------
class ResilientRedis:
    """
    Backwards-compatible delegator that mirrors your previous behavior:
    - Delegates calls to RedisUtils.client where possible
    - Catches Redis errors and returns safe defaults so the app continues
    """

    def __getattr__(self, name):
        # Resolve the real client attribute lazily to avoid import-time issues
        def _get_attr():
            client = get_redis_utils().client
            return getattr(client, name)

        attr = _get_attr()

        # Don't wrap pipeline/context manager
        if name == "pipeline":
            return attr

        if not callable(attr):
            return attr

        async def wrapper(*args, **kwargs):
            try:
                res = attr(*args, **kwargs)
                # If the result is awaitable, await it
                if hasattr(res, "__await__"):
                    return await res
                return res
            except Exception as e:
                logger.error("Redis operation '%s' failed: %s. Application will continue without Redis.", name, e)
                # Safe defaults for common operations used in the codebase
                if name in ("get", "getdel", "eval", "evalsha"):
                    return None
                if name == "smembers":
                    return set()
                if name == "incr":
                    return 1
                if name == "zcard":
                    return 0
                if name == "set":
                    return True
                if name == "delete":
                    return 0
                return None

        return wrapper


# Single shared resilient client for the app to import
redis_client = ResilientRedis()
