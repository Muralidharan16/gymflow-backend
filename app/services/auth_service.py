import uuid
import json
from datetime import datetime, timezone
from fastapi import HTTPException, status
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.models.organization import Organization
from app.models.gym import Gym
from app.models.staff import GymOwner
from app.models.enums import StaffRole, OrgTier
from app.repositories.gym_repo import GymRepository
from app.repositories.base import BaseRepository
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.redis import redis_client

class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def signup(self, data: SignupRequest) -> TokenResponse:
        """Atomic signup flow: Org -> Gym -> Owner."""
        # 1. Check if email exists (Check BEFORE creating any record)
        q = select(GymOwner).where(GymOwner.email == data.email)
        existing = await self.session.execute(q)
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already exists")

        async with self.session.begin():
            # Create Organization
            org = Organization(
                name=data.org_name,
                facility_type=data.facility_type,
                tier=OrgTier.basic,
                pan_number=None # Collected later
            )
            self.session.add(org)
            await self.session.flush()

            # Create Main Gym
            gym = Gym(
                org_id=org.id,
                name=f"{data.org_name} - Main",
                gymu_id=f"GYM-{str(uuid.uuid4())[:8].upper()}"
            )
            self.session.add(gym)
            await self.session.flush()

            # Create Owner
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
            
            payload = {
                "sub": str(owner.id),
                "org_id": str(org.id),
                "gym_id": str(gym.id),
                "role": owner.role.value
            }
            tokens = TokenResponse(
                access_token=create_access_token(payload),
                refresh_token=create_refresh_token(payload)
            )

            # Track session family for rotation tracking
            new_refresh_payload = decode_token(tokens.refresh_token)
            family_id = new_refresh_payload["f_id"]
            await redis_client.sadd(f"user_sessions:{str(owner.id)}", family_id)
            await redis_client.setex(f"family_latest:{family_id}", 86400 * 7, new_refresh_payload["jti"])

            return tokens

    async def login(self, data: LoginRequest) -> TokenResponse:
        """Secure login with credential verification and brute-force protection."""
        login_lock_key = f"login_lock:{data.email}"
        attempts_key = f"login_attempts:{data.email}"

        # Check if account is locked
        if await redis_client.get(login_lock_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Account locked due to multiple failed attempts. Try again later."
            )

        q = select(GymOwner).where(GymOwner.email == data.email)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()

        if not owner or not verify_password(data.password, owner.password_hash):
            # Increment failed attempts
            attempts = await redis_client.incr(attempts_key)
            if attempts == 1:
                await redis_client.expire(attempts_key, 600) # 10 min window
            
            if attempts >= 5:
                await redis_client.setex(login_lock_key, 1800, "1") # Lock for 30 mins
                await redis_client.delete(attempts_key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed attempts. Account locked for 30 minutes."
                )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail=f"Invalid credentials. {5 - attempts} attempts remaining."
            )

        # Successful login - reset attempts
        await redis_client.delete(attempts_key)

        if not owner.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="Account is deactivated"
            )

        payload = {
            "sub": str(owner.id),
            "org_id": str(owner.org_id),
            "gym_id": str(owner.gym_id) if owner.gym_id else None,
            "role": owner.role.value
        }
        tokens = TokenResponse(
            access_token=create_access_token(payload),
            refresh_token=create_refresh_token(payload)
        )

        # Track session family for logout_all and rotation tracking
        new_refresh_payload = decode_token(tokens.refresh_token)
        family_id = new_refresh_payload["f_id"]
        await redis_client.sadd(f"user_sessions:{str(owner.id)}", family_id)
        await redis_client.setex(f"family_latest:{family_id}", 86400 * 7, new_refresh_payload["jti"])

        return tokens

    async def refresh(self, data: RefreshRequest) -> TokenResponse:
        """
        Refresh access token using a refresh token.
        Implements token rotation and family tracking.
        """
        try:
            payload = decode_token(data.refresh_token)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")

        jti = payload.get("jti")
        family_id = payload.get("f_id")
        user_id = payload.get("sub")

        # 1. Check if the specific token is blacklisted
        if await redis_client.get(f"blacklist:{jti}"):
            raise HTTPException(status_code=401, detail="Token has been revoked")

        # 2. Check if the entire family is revoked (Token reuse detection)
        if await redis_client.get(f"family_revoked:{family_id}"):
            raise HTTPException(status_code=401, detail="Session has been revoked due to security breach")

        # 3. Check if this family ID was already used (Rotation check)
        # We store the latest JTI for each family. If current JTI != latest, it means theft.
        latest_jti = await redis_client.get(f"family_latest:{family_id}")
        if latest_jti and latest_jti != jti:
            # Token reuse detected! Revoke the whole family.
            await redis_client.setex(f"family_revoked:{family_id}", 86400 * 7, "1")
            raise HTTPException(status_code=401, detail="Token reuse detected. All sessions revoked.")

        # Blacklist the old refresh token (Rotation)
        exp = payload.get("exp")
        if exp is not None:
            now = datetime.now(timezone.utc).timestamp()
            ttl = int(exp - now) if exp > now else 0
            if ttl > 0:
                await redis_client.setex(f"blacklist:{jti}", ttl, "1")

        # Create new tokens (Keep the same family ID)
        new_payload = {
            "sub": payload["sub"],
            "org_id": payload["org_id"],
            "gym_id": payload.get("gym_id"),
            "role": payload["role"]
        }
        
        tokens = TokenResponse(
            access_token=create_access_token(new_payload),
            refresh_token=create_refresh_token(new_payload, family_id=family_id)
        )

        # Update latest JTI for the family
        new_refresh_payload = decode_token(tokens.refresh_token)
        await redis_client.setex(f"family_latest:{family_id}", 86400 * 7, new_refresh_payload["jti"])
        
        return tokens

    async def logout(self, token: str):
        """Revoke a single session family."""
        try:
            payload = decode_token(token)
            family_id = payload.get("f_id")
            if family_id:
                await redis_client.setex(f"family_revoked:{family_id}", 86400 * 7, "1")
            
            jti = payload.get("jti")
            exp = payload.get("exp")
            if exp is not None:
                now = datetime.now(timezone.utc).timestamp()
                ttl = int(exp - now) if exp > now else 0
                if ttl > 0:
                    await redis_client.setex(f"blacklist:{jti}", ttl, "1")
        except Exception:
            pass

    async def logout_all(self, user_id: str):
        """
        Revoke all sessions for a user.
        Requires tracking active families per user.
        """
        user_sessions_key = f"user_sessions:{user_id}"
        families = await redis_client.smembers(user_sessions_key)
        for f_id in families:
            await redis_client.setex(f"family_revoked:{f_id}", 86400 * 7, "1")
        await redis_client.delete(user_sessions_key)
