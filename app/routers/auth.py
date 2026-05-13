from fastapi import APIRouter, Depends, status, Request, Response as FastApiResponse, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from app.schemas.common import Response
from app.services.auth_service import AuthService
from app.core.config import settings


router = APIRouter(prefix="/auth", tags=["Authentication"])

def set_auth_cookies(response: FastApiResponse, tokens: TokenResponse):
    response.set_cookie(
        key="access_token",
        value=tokens.access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=15 * 60
    )
    response.set_cookie(
        key="refresh_token",
        value=tokens.refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60
    )

@router.post("/signup")
async def signup(request: Request, data: SignupRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    client_ip = request.client.host
    result = await service.signup(data, client_ip)
    return result

@router.get("/verify")
async def verify(token: str, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    result = await service.verify(token)
    
    if "error" in result:
        reason = result["error"]
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/verify-failed?reason={reason}")
    
    owner = result["owner"]
    org = result["org"]
    
    # Create tokens
    from app.core.security import create_access_token, create_refresh_token
    import hashlib
    from datetime import datetime, timezone, timedelta
    from app.models.auth import RefreshToken

    access_token = create_access_token(owner.id, org.id, owner.email)
    refresh_token = create_refresh_token(owner.id)
    
    # Store refresh token
    rt_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    db_rt = RefreshToken(
        owner_id=owner.id,
        token_hash=rt_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7)
    )
    db.add(db_rt)
    await db.commit()
    
    response = RedirectResponse(url=f"{settings.FRONTEND_URL}/onboarding")
    set_auth_cookies(response, TokenResponse(access_token=access_token, refresh_token=refresh_token))
    return response

@router.post("/resend-verification")
async def resend_verification(request: Request, data: dict, db: AsyncSession = Depends(get_db)):
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=422, detail="Email is required")
    service = AuthService(db)
    result = await service.resend_verification(email)
    return result

@router.post("/login")
async def login(response: FastApiResponse, data: LoginRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.login(data)
    set_auth_cookies(response, tokens)
    return {"status": "success", "message": "Login successful"}

@router.post("/refresh")
async def refresh(request: Request, response: FastApiResponse, db: AsyncSession = Depends(get_db)):
    # In a real app, refresh token should be read from cookie
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        # Fallback to body for some clients
        try:
            data = await request.json()
            refresh_token = data.get("refresh_token")
        except:
            pass
            
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing")
        
    service = AuthService(db)
    tokens = await service.refresh_token(refresh_token)
    set_auth_cookies(response, tokens)
    return {"status": "success", "message": "Token refreshed"}
