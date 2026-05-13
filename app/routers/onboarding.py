# app/routers/onboarding.py
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import decode_token
from app.schemas.onboarding import (
    OnboardingCompleteRequest, 
    PincodeLookupResponse, 
    OnboardingStatusResponse
)
from app.services.onboarding_service import OnboardingService
from app.services.pincode_service import PincodeService
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from fastapi import HTTPException

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

async def get_current_owner_id(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload = decode_token(token)
        owner_id = payload.get("sub")
        if not owner_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return owner_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")

@router.get("/pincode/{pincode}", response_model=PincodeLookupResponse)
async def pincode_lookup(pincode: str):
    """
    Look up address details by 6-digit Indian pincode.
    """
    service = PincodeService()
    return await service.lookup(pincode)

@router.post("/complete", status_code=status.HTTP_200_OK)
async def complete_onboarding(
    request: Request,
    data: OnboardingCompleteRequest,
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(get_current_owner_id)
):
    """
    Submit onboarding details to activate the account and start the free trial.
    """
    service = OnboardingService(db)
    ip_address = request.client.host
    user_agent = request.headers.get("user-agent", "")
    
    return await service.complete_onboarding(
        owner_id=owner_id, 
        data=data, 
        ip_address=ip_address, 
        user_agent=user_agent
    )

@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    db: AsyncSession = Depends(get_db),
    owner_id: str = Depends(get_current_owner_id)
):
    """
    Retrieve current onboarding and trial status.
    """
    service = OnboardingService(db)
    return await service.get_status(owner_id)
