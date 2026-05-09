import uuid
import logging
from typing import List, Optional
from app.core.redis import redis_client
from app.models.member import Member

logger = logging.getLogger(__name__)

class CacheService:
    @staticmethod
    async def invalidate_member_access(member_id: uuid.UUID, qr_token: Optional[str] = None, 
                                     member_uid: Optional[str] = None, 
                                     fingerprint_id: Optional[str] = None) -> None:
        """
        Invalidate all cached access decisions for a member.
        Called after subscription updates, freezes, or cancellations.
        """
        keys = []
        if qr_token:
            keys.append(f"{qr_token}:access")
        if member_uid:
            keys.append(f"{member_uid}:access")
        if fingerprint_id:
            keys.append(f"{fingerprint_id}:access")
            
        if keys:
            try:
                # Use pipeline for atomic multi-key delete
                async with redis_client.pipeline(transaction=True) as pipe:
                    for key in keys:
                        pipe.delete(key)
                    await pipe.execute()
                logger.info(f"Invalidated cache for member {member_id}")
            except Exception as e:
                logger.error(f"Failed to invalidate cache for member {member_id}: {e}")

    @staticmethod
    async def warm_access_cache(uid: str, data: dict, ttl: int = 43200) -> None:
        """Warm the access cache with a pre-computed decision."""
        try:
            import json
            await redis_client.setex(f"{uid}:access", ttl, json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to warm cache for UID {uid}: {e}")
