from fastapi import APIRouter, Depends, status, Request, Response as FastApiResponse, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from app.schemas.common import Response
from app.services.auth_service import AuthService
from app.core.redis import redis_client

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

@router.post("/signup", response_model=Response[TokenResponse])
async def signup(request: Request, response: FastApiResponse, data: SignupRequest, db: AsyncSession = Depends(get_db)):
    # Rate limit: Max 5 attempts per IP per 10 mins
    client_ip = request.client.host
    rate_limit_key = f"signup_limit:{client_ip}"
    attempts = await redis_client.incr(rate_limit_key)
    if attempts == 1:
        await redis_client.expire(rate_limit_key, 600)
    if attempts > 5:
        ttl = await redis_client.ttl(rate_limit_key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {max(1, ttl // 60)} minutes."
        )

    service = AuthService(db)
    tokens = await service.signup(data)
    set_auth_cookies(response, tokens)
    return Response(data=tokens, message="Signup successful")

@router.post("/login", response_model=Response[TokenResponse])
async def login(response: FastApiResponse, data: LoginRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.login(data)
    set_auth_cookies(response, tokens)
    return Response(data=tokens, message="Login successful")

@router.post("/refresh", response_model=Response[TokenResponse])
async def refresh(response: FastApiResponse, data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.refresh(data)
    set_auth_cookies(response, tokens)
    return Response(data=tokens, message="Token refreshed")

@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        service = AuthService(db)
        await service.logout(token)
    return {"message": "Logged out successfully"}

@router.post("/logout-all")
async def logout_all(request: Request, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    await service.logout_all(request.state.staff_id)
    return {"message": "All sessions revoked"}
