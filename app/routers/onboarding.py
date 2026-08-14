# app/routers/onboarding.py
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.auth_database import get_auth_db
from app.core.redis import redis_client
from app.core.security import decode_token
from app.repositories.geo_repository import GeoRepository
from app.schemas.onboarding import (
    OnboardingCompleteRequest,
    OnboardingStatusResponse,
    PincodeLookupResponse,
)
from app.services.geo_service import GeoService
from app.services.onboarding_service import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


async def get_current_owner_id(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
) -> str:
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = decode_token(token)
        owner_id = payload.get("sub")
        if not owner_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return owner_id
    except Exception:
        raise HTTPException(status_code=401, detail="Could not validate credentials")


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
    owner_id: str = Depends(get_current_owner_id),
):
    """Activate an owner/tenant through the dedicated bootstrap DB identity."""
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
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(get_current_owner_id),
):
    service = OnboardingService(db)
    return await service.get_status(owner_id)