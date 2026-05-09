import asyncio
import time
from typing import Optional
from app.core.redis import redis_client

class RedisLock:
    """
    Simple distributed lock using Redis SET NX.
    """
    def __init__(self, name: str, timeout: int = 10, expiry: int = 30):
        self.name = f"lock:{name}"
        self.timeout = timeout
        self.expiry = expiry
        self.token = str(time.time())

    async def __aenter__(self):
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            if await redis_client.set(self.name, self.token, ex=self.expiry, nx=True):
                return self
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Could not acquire lock for {self.name}")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Only release if we own it (token match)
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await redis_client.eval(script, 1, self.name, self.token)
