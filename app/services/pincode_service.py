# app/services/pincode_service.py
import httpx
import json
import logging
from fastapi import HTTPException, status
from app.core.config import settings
from app.core.redis import get_redis_utils

logger = logging.getLogger(__name__)

class PincodeService:
    def __init__(self):
        self.redis_utils = get_redis_utils()
        self.client = self.redis_utils.client
        self.india_post_url = "https://api.postalpincode.in/pincode/{pincode}"

    async def lookup(self, pincode: str) -> dict:
        """
        Lookup city/state by pincode. 
        Uses India Post API with 7-day Redis caching.
        """
        if not pincode or len(pincode) != 6 or not pincode.isdigit():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid pincode format. Must be 6 digits."
            )

        cache_key = f"cache:pincode:{pincode}"
        
        # 1. Check cache
        try:
            cached_data = await self.client.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
        except Exception:
            logger.warning("Redis cache lookup failed for pincode %s", pincode)

        # 2. API Lookup
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                response = await client.get(self.india_post_url.format(pincode=pincode))
                response.raise_for_status()
                data = response.json()
                
                if not data or data[0]["Status"] != "Success":
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"No data found for pincode {pincode}"
                    )
                
                post_offices = data[0]["PostOffice"]
                if not post_offices:
                    raise HTTPException(status_code=404, detail="No post office found")
                
                # Take the first entry as primary
                primary = post_offices[0]
                result = {
                    "city": primary["District"],
                    "state": primary["State"],
                    "district": primary["District"]
                }
                
                # 3. Cache result for 7 days
                try:
                    await self.client.setex(cache_key, 604800, json.dumps(result))
                except Exception:
                    logger.warning("Failed to cache pincode %s", pincode)
                
                return result

            except httpx.TimeoutException:
                logger.error("India Post API timeout for pincode %s", pincode)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Address lookup service is currently slow. Please enter manually."
                )
            except Exception as e:
                logger.exception("Unexpected error during pincode lookup")
                # Don't block onboarding if API is down, but return error for frontend to handle
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Address lookup service unavailable. Please enter details manually."
                )
