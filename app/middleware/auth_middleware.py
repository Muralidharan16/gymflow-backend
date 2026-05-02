from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from ..config import settings
from ..database import get_db
from ..models.models import GymOwner

security = HTTPBearer(auto_error=False)


async def get_current_owner(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    owner_id = payload.get('sub')
    gym_id = payload.get('gym_id')
    if owner_id is None or gym_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token")

    # fetch owner
    owner = await db.get(GymOwner, owner_id)
    if owner is None or str(owner.gym_id) != str(gym_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Owner not found or not in gym")

    return owner
