import uuid
import json
from datetime import datetime, timezone
import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.browser_session import mark_family_revoked
from app.core.redis import get_redis_utils
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_password,
    verify_token,
)
from app.models.auth import Owner
from app.models.auth_session import AuthSession, AuthSessionFamily
from app.models.gym import Gym, FacilityType
from app.models.organization import Organization
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse
from app.utils.email_utils import send_verification_email
from app.utils.slug import generate_slug

import hashlib
import hmac
import secrets

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def signup(self, data: SignupRequest, ip_address: str) -> dict:
        """
        Validate data, store in Redis, and send magic link.
        Does NOT write to PostgreSQL.
        """
        redis_utils = get_redis_utils()
        email = data.email.strip().lower()

        rate_key = f"ratelimit:signup:{ip_address}"
        if await redis_utils.is_rate_limited(rate_key, limit=5, ttl=600):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many signup attempts. Try again in 10 minutes.",
            )

        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        if result.scalar_one_or_none():
            return {
                "status": "success",
                "message": "Verification email sent. Please check your inbox.",
            }

        hashed_pw = hash_password(data.password)
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        signup_poll_token = secrets.token_urlsafe(32)
        signup_poll_token_hash = hashlib.sha256(signup_poll_token.encode("utf-8")).hexdigest()

        pending_key = f"signup:pending:{token_hash}"
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        email_lookup_key = f"signup:email:{email_hash}"

        signup_data = {
            "org_name": data.org_name,
            "owner_name": data.owner_name,
            "email": email,
            "hashed_password": hashed_pw,
            "facility_type": data.facility_type,
            "signup_poll_token_hash": signup_poll_token_hash,
            "resend_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await redis_utils.set_json_with_ttl(pending_key, signup_data, ttl=600)
            await redis_utils.client.set(email_lookup_key, token_hash, ex=600)
        except Exception:
            logger.exception("Redis failure during signup")
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        email_sent = await send_verification_email(
            email=email,
            owner_name=data.owner_name,
            org_name=data.org_name,
            raw_token=raw_token,
        )

        if not email_sent:
            await redis_utils.delete_keys_safe([pending_key, email_lookup_key])
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        return {
            "status": "success",
            "message": "Verification email sent. Please check your inbox.",
            "signup_poll_token": signup_poll_token,
        }

    @staticmethod
    def verify_signup_poll_token(pending_data: dict, poll_token: str) -> bool:
        expected_hash = pending_data.get("signup_poll_token_hash")
        if not expected_hash or not poll_token:
            return False
        actual_hash = hashlib.sha256(poll_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected_hash, actual_hash)

    async def verify(self, token: str) -> dict:
        """Validate a magic token and atomically create the tenant account."""
        redis_utils = get_redis_utils()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        pending_key = f"signup:pending:{token_hash}"

        raw_payload = await redis_utils.client.get(pending_key)
        if not raw_payload:
            return {"error": "expired"}

        data = json.loads(raw_payload)
        email = data["email"].strip().lower()
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        email_lookup_key = f"signup:email:{email_hash}"

        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        existing_owner = result.scalar_one_or_none()
        if existing_owner:
            await redis_utils.delete_keys_safe([pending_key, email_lookup_key])
            return {"error": "already_registered"}

        org = None
        owner = None
        try:
            async with self.session.begin_nested():
                base_slug = generate_slug(data["org_name"])
                slug = base_slug
                counter = 1
                while True:
                    slug_exists_q = select(Organization).where(Organization.slug == slug)
                    slug_exists_res = await self.session.execute(slug_exists_q)
                    if not slug_exists_res.scalar_one_or_none():
                        break
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                org = Organization(
                    name=data["org_name"],
                    slug=slug,
                    business_type=data["facility_type"],
                )
                self.session.add(org)
                await self.session.flush()

                gym = Gym(
                    org_id=org.id,
                    name=data["org_name"],
                    facility_type=data["facility_type"],
                    gymu_id=f"GYM-{str(uuid.uuid4())[:8].upper()}",
                )
                facility_type_q = select(FacilityType).where(
                    FacilityType.system_name == data["facility_type"]
                )
                facility_type_res = await self.session.execute(facility_type_q)
                primary_type = facility_type_res.scalar_one_or_none()
                if primary_type:
                    gym.facility_types.append(primary_type)
                self.session.add(gym)

                owner = Owner(
                    org_id=org.id,
                    owner_name=data["owner_name"],
                    email=email,
                    hashed_password=data["hashed_password"],
                    email_verified=True,
                )
                self.session.add(owner)
                await self.session.flush()

            await self.session.commit()
        except Exception:
            logger.exception("Atomic account creation failed")
            await self.session.rollback()
            return {"error": "server_error"}

        await redis_utils.delete_keys_safe([pending_key, email_lookup_key])
        return {
            "owner": owner,
            "org": org,
            "signup_poll_token_hash": data.get("signup_poll_token_hash"),
        }

    async def resend_verification(self, email: str) -> dict:
        """Resend a magic link if within the anti-abuse limits."""
        redis_utils = get_redis_utils()
        email = email.strip().lower()
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()

        resend_rate_key = f"ratelimit:resend:{email_hash}"
        if await redis_utils.is_rate_limited(resend_rate_key, limit=3, ttl=3600):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Maximum resend limit reached. Please restart signup after 10 minutes.",
            )

        email_lookup_key = f"signup:email:{email_hash}"
        old_token_hash = await redis_utils.client.get(email_lookup_key)
        if not old_token_hash:
            return {"status": "success", "message": "Verification email resent."}

        pending_key = f"signup:pending:{old_token_hash}"
        raw_data = await redis_utils.get_json(pending_key)
        if not raw_data:
            return {"status": "success", "message": "Verification email resent."}

        if raw_data.get("resend_count", 0) >= 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Maximum resend limit reached. Please restart signup after 10 minutes.",
            )

        new_raw_token = secrets.token_urlsafe(48)
        new_token_hash = hashlib.sha256(new_raw_token.encode("utf-8")).hexdigest()
        new_data = {**raw_data, "resend_count": raw_data.get("resend_count", 0) + 1}
        new_pending_key = f"signup:pending:{new_token_hash}"

        try:
            await redis_utils.set_json_with_ttl(new_pending_key, new_data, ttl=600)
            await redis_utils.client.set(email_lookup_key, new_token_hash, ex=600)
        except Exception:
            logger.exception("Redis failure during resend")
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        email_sent = await send_verification_email(
            email=new_data["email"],
            owner_name=new_data["owner_name"],
            org_name=new_data["org_name"],
            raw_token=new_raw_token,
        )

        if not email_sent:
            await redis_utils.delete_keys_safe([new_pending_key])
            await redis_utils.client.set(email_lookup_key, old_token_hash, ex=600)
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        await redis_utils.delete_keys_safe([pending_key])
        return {"status": "success", "message": "Verification email resent."}

    @staticmethod
    def _tokens_for_owner(owner: Owner, family_id: uuid.UUID) -> TokenResponse:
        return TokenResponse(
            access_token=create_access_token(
                owner.id,
                owner.org_id,
                owner.email,
                family_id=str(family_id),
            ),
            refresh_token=create_refresh_token(owner.id, family_id=str(family_id)),
            onboarding_completed=owner.onboarding_completed,
        )

    async def issue_session(self, owner: Owner) -> TokenResponse:
        """Create a new durable browser session family and family-bound token pair."""
        family = AuthSessionFamily(org_id=owner.org_id, user_id=owner.id)
        self.session.add(family)
        await self.session.flush()

        tokens = self._tokens_for_owner(owner, family.id)
        self.session.add(
            AuthSession(
                user_id=owner.id,
                org_id=owner.org_id,
                token_family_id=family.id,
                refresh_token_hash=hash_token(tokens.refresh_token),
                token_version_snapshot=1,
            )
        )
        await self.session.commit()
        return tokens

    async def login(self, data: LoginRequest) -> TokenResponse:
        """Authenticate an owner and create a family-bound browser session."""
        email = data.email.strip().lower()
        login_lock_key = f"login_lock:{email}"
        attempts_key = f"login_attempts:{email}"
        redis_utils = get_redis_utils()

        if await redis_utils.client.get(login_lock_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Account locked due to multiple failed attempts. Try again later.",
            )

        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()

        if not owner or not verify_password(data.password, owner.hashed_password):
            attempts = await redis_utils.client.incr(attempts_key)
            if attempts == 1:
                await redis_utils.client.expire(attempts_key, 600)
            if attempts >= 5:
                await redis_utils.client.setex(login_lock_key, 1800, "1")
                await redis_utils.client.delete(attempts_key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed attempts. Account locked for 30 minutes.",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid credentials. {5 - attempts} attempts remaining.",
            )

        if not owner.email_verified:
            raise HTTPException(status_code=403, detail="Email not verified")

        await redis_utils.client.delete(attempts_key)
        return await self.issue_session(owner)

    async def _revoke_family(
        self,
        family: AuthSessionFamily,
        *,
        reused_session: AuthSession | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        family.revoked_at = family.revoked_at or now
        if reused_session is not None:
            reused_session.reuse_detected_at = reused_session.reuse_detected_at or now
            reused_session.compromised_at = reused_session.compromised_at or now
        await self.session.commit()
        try:
            await mark_family_revoked(str(family.id))
        except Exception:
            # DB revocation is authoritative. Redis is a fast rejection signal only.
            logger.exception("Failed to publish session-family revocation marker")

    async def refresh_token(self, raw_refresh_token: str) -> TokenResponse:
        """Rotate a refresh token under a row lock and detect replay/reuse."""
        try:
            payload = verify_token(raw_refresh_token, "refresh")
            subject_id = uuid.UUID(str(payload.get("sub")))
            claimed_family_raw = payload.get("f_id")
            claimed_family_id = (
                uuid.UUID(str(claimed_family_raw))
                if claimed_family_raw is not None
                else None
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid refresh token claims") from exc
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token") from exc

        token_hash = hash_token(raw_refresh_token)
        result = await self.session.execute(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == token_hash)
            .with_for_update()
        )
        db_session = result.scalar_one_or_none()
        if db_session is None:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

        family_result = await self.session.execute(
            select(AuthSessionFamily)
            .where(AuthSessionFamily.id == db_session.token_family_id)
            .with_for_update()
        )
        family = family_result.scalar_one_or_none()
        if family is None:
            raise HTTPException(status_code=401, detail="Session family missing")

        if db_session.revoked_at is not None:
            await self._revoke_family(family, reused_session=db_session)
            raise HTTPException(status_code=401, detail="Session compromised. Please login again.")

        if family.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Session revoked")

        if subject_id != db_session.user_id:
            await self._revoke_family(family, reused_session=db_session)
            raise HTTPException(status_code=401, detail="Session compromised. Please login again.")

        # Legacy DB-backed refresh tokens without f_id may rotate exactly once.
        # All newly-issued tokens are family-bound.
        if claimed_family_id is not None and claimed_family_id != family.id:
            await self._revoke_family(family, reused_session=db_session)
            raise HTTPException(status_code=401, detail="Session compromised. Please login again.")

        owner_result = await self.session.execute(select(Owner).where(Owner.id == db_session.user_id))
        owner = owner_result.scalar_one_or_none()
        if owner is None:
            await self._revoke_family(family, reused_session=db_session)
            raise HTTPException(status_code=401, detail="User not found")

        tokens = self._tokens_for_owner(owner, family.id)
        now = datetime.now(timezone.utc)
        db_session.revoked_at = now

        replacement = AuthSession(
            user_id=owner.id,
            org_id=owner.org_id,
            token_family_id=family.id,
            parent_session_id=db_session.id,
            refresh_token_hash=hash_token(tokens.refresh_token),
            token_version_snapshot=db_session.token_version_snapshot + 1,
        )
        self.session.add(replacement)
        await self.session.flush()
        db_session.replaced_by_session_id = replacement.id
        await self.session.commit()
        return tokens

    async def revoke_refresh_family(self, raw_refresh_token: str | None) -> str | None:
        """Revoke the durable family identified by a refresh cookie, even if JWT-expired."""
        if not raw_refresh_token:
            return None

        result = await self.session.execute(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == hash_token(raw_refresh_token))
            .with_for_update()
        )
        db_session = result.scalar_one_or_none()
        if db_session is None:
            return None

        family_result = await self.session.execute(
            select(AuthSessionFamily)
            .where(AuthSessionFamily.id == db_session.token_family_id)
            .with_for_update()
        )
        family = family_result.scalar_one_or_none()
        if family is None:
            return None

        await self._revoke_family(family)
        return str(family.id)
