import json
import logging
from typing import List, Optional
from redis.asyncio import Redis
from app.schemas.geo import PostalLookupResult, CountryDTO
from app.repositories.geo_repository import GeoRepository

logger = logging.getLogger(__name__)

class GeoService:
    def __init__(self, repo: GeoRepository, redis: Redis):
        self.repo = repo
        self.redis = redis

    async def get_cache_version(self, country_iso2: str) -> str:
        """Retrieves the atomic version for O(1) cache invalidation."""
        version = await self.redis.get(f"geo:version:{country_iso2}")
        return version.decode() if version else "1"

    async def lookup_postal_code(self, country_iso2: str, postal_code: str) -> List[PostalLookupResult]:
        """
        Looks up a postal code utilizing the versioned cache strategy.
        Cache Key: geo:v:{version}:lookup:{iso2}:{postal}
        """
        # Normalize inputs
        country_iso2 = country_iso2.upper()
        postal_code = postal_code.strip()
        
        version = await self.get_cache_version(country_iso2)
        cache_key = f"geo:v{version}:lookup:{country_iso2}:{postal_code}"
        
        cached = await self.redis.get(cache_key)
        if cached:
            # Emit cache hit metrics here if Prometheus was hooked
            return [PostalLookupResult(**item) for item in json.loads(cached)]
            
        # Cache Miss - Execute DB Lookup
        rows = await self.repo.get_postal_lookups(country_iso2, postal_code)
        
        results = []
        for p, c, s, co in rows:
            # Timezone Cascade Resolution
            resolved_timezone = p.timezone or c.timezone or s.timezone or co.timezone
            
            results.append(PostalLookupResult(
                postal_code_id=p.id,
                postal_code=p.postal_code,
                locality=p.locality,
                city_id=c.id,
                city_name=c.name,
                subdivision_id=s.id,
                subdivision_name=s.name,
                country_iso2=co.iso2,
                timezone=resolved_timezone,
                latitude=float(p.latitude) if p.latitude else None,
                longitude=float(p.longitude) if p.longitude else None
            ))
            
        # Only cache if results exist
        if results:
            await self.redis.setex(cache_key, 86400, json.dumps([r.model_dump() for r in results]))
            
        return results

    async def get_active_countries(self) -> List[CountryDTO]:
        countries = await self.repo.get_countries()
        return [
            CountryDTO(
                iso2=c.iso2,
                name=c.name,
                phone_code=c.phone_code,
                ui_config=c.ui_config
            ) for c in countries
        ]
