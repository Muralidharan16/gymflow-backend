# app/routers/onboarding.py
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_database import get_auth_db
from app.core.database import get_db
from app.core.redis import redis_client
from app.repositories.geo_repository import GeoRepository
from app.schemas.onboarding import (
    OnboardingCompleteRequest,
    OnboardingStatusResponse,
    PincodeLookupResponse,
)
from app.services.geo_service import GeoService
from app.services.onboarding_service import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


def get_current_owner_id(request: Request) -> str:
    """Use the centrally verified access-token principal from TenantMiddleware."""
    if getattr(request.state, "principal_type", None) != "owner":
        raise HTTPException(status_code=403, detail="Owner session required")
    owner_id = getattr(request.state, "staff_id", None)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(owner_id)


@router.get("/pincode/{pincode}", response_model=PincodeLookupResponse)
async def pincode_lookup(
    pincode: str,
    country: str = Query(default="IN", min_length=2, max_length=2),
    db: AsyncSession = Depends(get_db),
):
    repo = GeoRepository(db)
    service = GeoService(repo, redis_client)
    results = await service.lookup_postal_code(country.upper(), pincode)

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"Pincode {pincode} not found. Please enter city and state manually.",
        )

    first = results[0]
    return PincodeLookupResponse(
        city=first.city_name,
        state=first.subdivision_name,
        district=first.city_name,
    )


@router.post("/complete", status_code=status.HTTP_200_OK)
async def complete_onboarding(
    request: Request,
    data: OnboardingCompleteRequest,
    db: AsyncSession = Depends(get_auth_db),
):
    """Activate an owner/tenant through the dedicated bootstrap DB identity."""
    owner_id = get_current_owner_id(request)
    service = OnboardingService(db)
    ip_address = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent", "")
    return await service.complete_onboarding(
        owner_id=owner_id,
        data=data,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    owner_id = get_current_owner_id(request)
    service = OnboardingService(db)
    return await service.get_status(owner_id)
