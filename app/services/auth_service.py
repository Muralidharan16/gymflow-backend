import uuid
from fastapi import HTTPException, status
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token
from app.models.organization import Organization
from app.models.gym import Gym
from app.models.staff import GymOwner
from app.models.enums import StaffRole, OrgTier
from app.repositories.gym_repo import GymRepository
from app.repositories.base import BaseRepository
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def signup(self, data: SignupRequest) -> TokenResponse:
        # Atomic create: Org -> Gym -> Owner
        org = Organization(
            name=data.org_name,
            pan_number=data.pan_number,
            tier=OrgTier.basic
        )
        self.session.add(org)
        await self.session.flush()

        gym = Gym(
            org_id=org.id,
            name=f"{data.org_name} - Main",
            gymu_id=f"GYM-{str(uuid.uuid4())[:8].upper()}"
        )
        self.session.add(gym)
        await self.session.flush()

        owner = GymOwner(
            org_id=org.id,
            gym_id=gym.id,
            name=data.owner_name,
            email=data.email,
            password_hash=hash_password(data.password),
            role=StaffRole.owner,
            is_active=True,
            is_verified=True
        )
        self.session.add(owner)
        await self.session.commit()

        payload = {
            "sub": str(owner.id),
            "org_id": str(org.id),
            "gym_id": str(gym.id),
            "role": owner.role.value
        }
        return TokenResponse(
            access_token=create_access_token(payload),
            refresh_token=create_refresh_token(payload)
        )

    async def login(self, data: LoginRequest) -> TokenResponse:
        q = select(GymOwner).where(GymOwner.email == data.email)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()

        if not owner or not verify_password(data.password, owner.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        payload = {
            "sub": str(owner.id),
            "org_id": str(owner.org_id),
            "gym_id": str(owner.gym_id) if owner.gym_id else None,
            "role": owner.role.value
        }
        return TokenResponse(
            access_token=create_access_token(payload),
            refresh_token=create_refresh_token(payload)
        )
