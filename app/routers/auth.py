from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext
from ..config import settings
from ..database import get_db
from ..schemas.auth import LoginRequest, TokenResponse, RefreshRequest
from ..models.models import GymOwner

router = APIRouter(prefix="/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire, "typ": "refresh"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


@router.post('/login', response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        "SELECT * FROM gym_owners WHERE gym_id = :gym_id AND email = :email",
        {"gym_id": payload.gym_id, "email": payload.email},
    )
    # Use ORM query for safety
    owner = await db.execute(
        "SELECT id, password_hash FROM gym_owners WHERE gym_id = :gym_id AND email = :email",
        {"gym_id": payload.gym_id, "email": payload.email},
    )
    row = owner.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    owner_id = row[0]
    password_hash = row[1]

    if not pwd_context.verify(payload.password, password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    access_token = create_access_token({"sub": str(owner_id), "gym_id": payload.gym_id}, access_expires)
    refresh_token = create_refresh_token({"sub": str(owner_id), "gym_id": payload.gym_id}, refresh_expires)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_at=(datetime.utcnow() + access_expires))


@router.post('/refresh', response_model=TokenResponse)
async def refresh_token(payload: RefreshRequest):
    try:
        data = jwt.decode(payload.refresh_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if data.get('typ') != 'refresh':
            raise JWTError('Not a refresh token')
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid refresh token')

    owner_id = data.get('sub')
    gym_id = data.get('gym_id')
    access_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    access_token = create_access_token({"sub": str(owner_id), "gym_id": gym_id}, access_expires)
    refresh_token = create_refresh_token({"sub": str(owner_id), "gym_id": gym_id}, refresh_expires)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_at=(datetime.utcnow() + access_expires))
