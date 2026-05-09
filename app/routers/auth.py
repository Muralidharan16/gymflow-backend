from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from app.schemas.common import Response
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/signup", response_model=Response[TokenResponse])
async def signup(data: SignupRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.signup(data)
    return Response(data=tokens, message="Signup successful")

@router.post("/login", response_model=Response[TokenResponse])
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.login(data)
    return Response(data=tokens, message="Login successful")

@router.post("/refresh")
async def refresh(data: RefreshRequest):
    # Logic for refresh token
    return {"message": "Not implemented"}

@router.post("/logout")
async def logout():
    # Logic for logout/blacklist
    return {"message": "Logged out"}
