import json
from typing import Optional
import logging
import redis.asyncio as aioredis
from .config import settings

logger = logging.getLogger(__name__)
redis_client: Optional[aioredis.Redis] = None
redis_available: bool = False


async def init_redis() -> None:
    global redis_client, redis_available
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_keepalive=True)
        await redis_client.ping()
        redis_available = True
        logger.info("Redis connected successfully")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}. Running without Redis (rate limiting and pub/sub disabled)")
        redis_client = None
        redis_available = False


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.close()


async def publish_channel(channel: str, message: dict) -> int:
    global redis_client, redis_available
    if not redis_available or redis_client is None:
        logger.debug(f"Redis unavailable, skipping publish to {channel}")
        return 0
    try:
        return await redis_client.publish(channel, json.dumps(message))
    except Exception as e:
        logger.warning(f"Publish failed: {e}")
        return 0


async def rate_limit(key: str, limit: int, period_seconds: int = 60) -> bool:
    """Simple fixed-window rate limiting using Redis INCR and EXPIRE. Returns True if under limit (or Redis unavailable)."""
    global redis_client, redis_available
    if not redis_available or redis_client is None:
        logger.debug(f"Redis unavailable, skipping rate limit for {key}")
        return True  # Allow request if Redis not available
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, period_seconds)
        return count <= limit
    except Exception as e:
        logger.warning(f"Rate limit check failed: {e}, allowing request")
        return True  # Allow request on error
