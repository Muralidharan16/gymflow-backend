import redis.asyncio as aioredis
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

_real_client = aioredis.from_url(
    settings.REDIS_URL, encoding="utf-8", decode_responses=True
)

class ResilientRedis:
    def __getattr__(self, name):
        # Delegate to the real client
        attr = getattr(_real_client, name)
        
        # Don't wrap pipeline as it's a context manager and has its own flow
        if name == 'pipeline':
            return attr
            
        if not callable(attr):
            return attr
            
        async def wrapper(*args, **kwargs):
            try:
                res = attr(*args, **kwargs)
                if hasattr(res, "__await__"):
                    return await res
                return res
            except Exception as e:
                logger.error(f"Redis operation '{name}' failed: {e}. Application will continue without Redis.")
                # Return safe defaults to prevent app crashes
                if name == 'get': return None
                if name == 'smembers': return set()
                if name == 'incr': return 1
                if name == 'zcard': return 0
                return True
        return wrapper

redis_client = ResilientRedis()
