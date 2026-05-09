import time
from typing import Optional
from app.core.redis import redis_client

class RateLimiter:
    """
    Redis-based sliding window rate limiter.
    """
    def __init__(self, prefix: str, limit: int, window: int):
        self.prefix = prefix
        self.limit = limit
        self.window = window

    async def is_allowed(self, identifier: str) -> bool:
        """
        Check if the identifier is within the rate limit.
        """
        now = time.time()
        key = f"ratelimit:{self.prefix}:{identifier}"
        
        # Remove old requests outside the window
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now - self.window)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, self.window)
            _, count, _, _ = await pipe.execute()
        
        return count < self.limit

    async def get_remaining(self, identifier: str) -> int:
        key = f"ratelimit:{self.prefix}:{identifier}"
        count = await redis_client.zcard(key)
        return max(0, self.limit - count)
