from fastapi import APIRouter, Depends, status, Request
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

@router.post("/refresh", response_model=Response[TokenResponse])
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    tokens = await service.refresh(data)
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
