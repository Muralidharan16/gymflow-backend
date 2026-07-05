from fastapi import APIRouter, Depends, Query, HTTPException
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.redis import redis_client
from app.repositories.geo_repository import GeoRepository
from app.services.geo_service import GeoService
from app.schemas.geo import PostalLookupResult, CountryDTO

router = APIRouter(prefix="/v1/geo", tags=["Geo Infrastructure"])

async def get_geo_service(db: AsyncSession = Depends(get_db)) -> GeoService:
    repo = GeoRepository(db)
    return GeoService(repo, redis_client)

@router.get("/countries", response_model=List[CountryDTO])
async def get_countries(
    service: GeoService = Depends(get_geo_service)
):
    """
    Returns all active countries configured in the platform.
    Used for populating frontend dropdowns and fetching UI config.
    """
    return await service.get_active_countries()

@router.get("/lookup", response_model=List[PostalLookupResult])
async def lookup_postal(
    country_iso2: str = Query(..., min_length=2, max_length=2, description="2-letter ISO country code"),
    postal_code: str = Query(..., min_length=1, max_length=20, description="Postal code to lookup"),
    service: GeoService = Depends(get_geo_service)
):
    """
    Looks up canonical geographic entities for a given postal code.
    May return multiple localities for the same postal code.
    Utilizes O(1) versioned Redis caching.
    """
    results = await service.lookup_postal_code(country_iso2, postal_code)
    if not results:
        raise HTTPException(status_code=404, detail="Postal code not found or inactive")
    return results

@router.get("/autocomplete", response_model=List[str])
async def autocomplete(
    query: str = Query(..., min_length=2),
    service: GeoService = Depends(get_geo_service)
):
    """
    Phase 3 stub: Will proxy to geo_search_projection
    """
    return []
