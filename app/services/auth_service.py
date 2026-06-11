import uuid
import json
from datetime import datetime, timezone, timedelta
import logging
from fastapi import HTTPException, status
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.models.organization import Organization
from app.models.gym import Gym, FacilityType
from app.models.auth import Owner
from app.models.auth_session import AuthSession, AuthSessionFamily
from app.models.enums import StaffRole, OrgTier
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.redis import get_redis_utils
from app.utils.email_utils import send_verification_email
import secrets
import hashlib
import hmac
from app.utils.slug import generate_slug

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

        # 1. Rate limiting by IP
        rate_key = f"ratelimit:signup:{ip_address}"
        if await redis_utils.is_rate_limited(rate_key, limit=5, ttl=600):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many signup attempts. Try again in 10 minutes."
            )

        # 2. Check email existence (anti-enumeration: return success even if exists)
        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        if result.scalar_one_or_none():
            return {
                "status": "success",
                "message": "Verification email sent. Please check your inbox."
            }

        # 3. Hash password
        hashed_pw = hash_password(data.password)

        # 4. Generate magic link token
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        signup_poll_token = secrets.token_urlsafe(32)
        signup_poll_token_hash = hashlib.sha256(signup_poll_token.encode("utf-8")).hexdigest()

        # 5. Store in Redis
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
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        try:
            await redis_utils.set_json_with_ttl(pending_key, signup_data, ttl=600)
            await redis_utils.client.set(email_lookup_key, token_hash, ex=600)
        except Exception:
            logger.exception("Redis failure during signup")
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        # 6. Send verification email
        email_sent = await send_verification_email(
            email=email,
            owner_name=data.owner_name,
            org_name=data.org_name,
            raw_token=raw_token
        )

        if not email_sent:
            # Cleanup Redis on email failure
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
        """
        Validate token, create account atomically, and return owner/org data for JWT issuance.
        """
        redis_utils = get_redis_utils()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        pending_key = f"signup:pending:{token_hash}"

        # Read first; consume the token only after account creation commits.
        raw_payload = await redis_utils.client.get(pending_key)
        if not raw_payload:
            return {"error": "expired"}

        data = json.loads(raw_payload)
        email = data["email"].strip().lower()
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        email_lookup_key = f"signup:email:{email_hash}"

        # 2. Check if already registered (race condition guard)
        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        existing_owner = result.scalar_one_or_none()
        if existing_owner:
            await redis_utils.delete_keys_safe([pending_key, email_lookup_key])
            return {"error": "already_registered"}

        # 3. Atomic account creation
        # FIX: The SELECT above already opened a transaction on this session.
        # Use begin_nested() (SAVEPOINT) instead of begin() to avoid
        # "A transaction is already begun on this Session" error.
        org = None
        owner = None
        try:
            async with self.session.begin_nested():
                # a. Create Organization
                base_slug = generate_slug(data["org_name"])
                
                # Check for slug collision
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
                    business_type=data["facility_type"]
                )
                self.session.add(org)
                await self.session.flush()  # get org.id

                # b. Create Gym
                gym = Gym(
                    org_id=org.id,
                    name=data["org_name"],
                    facility_type=data["facility_type"],
                    gymu_id=f"GYM-{str(uuid.uuid4())[:8].upper()}"
                )
                # Link to FacilityType reference table
                facility_type_q = select(FacilityType).where(FacilityType.system_name == data["facility_type"])
                facility_type_res = await self.session.execute(facility_type_q)
                primary_type = facility_type_res.scalar_one_or_none()
                if primary_type:
                    gym.facility_types.append(primary_type)
                    
                self.session.add(gym)

                # c. Create Owner
                owner = Owner(
                    org_id=org.id,
                    owner_name=data["owner_name"],
                    email=email,
                    hashed_password=data["hashed_password"],
                    email_verified=True
                )
                self.session.add(owner)
                await self.session.flush()  # get owner.id

            # Commit the outer transaction (the one opened by the SELECT above)
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
        """
        Resend magic link if within limits.
        """
        redis_utils = get_redis_utils()
        email = email.strip().lower()
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()

        # 1. Rate limiting (3 per hour per email)
        resend_rate_key = f"ratelimit:resend:{email_hash}"
        if await redis_utils.is_rate_limited(resend_rate_key, limit=3, ttl=3600):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Maximum resend limit reached. Please restart signup after 10 minutes."
            )

        # 2. Lookup existing pending signup
        email_lookup_key = f"signup:email:{email_hash}"
        old_token_hash = await redis_utils.client.get(email_lookup_key)
        if not old_token_hash:
            # Anti-enumeration
            return {"status": "success", "message": "Verification email resent."}

        pending_key = f"signup:pending:{old_token_hash}"
        raw_data = await redis_utils.get_json(pending_key)
        if not raw_data:
            return {"status": "success", "message": "Verification email resent."}

        # 3. Check resend count
        if raw_data.get("resend_count", 0) >= 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Maximum resend limit reached. Please restart signup after 10 minutes."
            )

        # 4. Generate new token
        new_raw_token = secrets.token_urlsafe(48)
        new_token_hash = hashlib.sha256(new_raw_token.encode("utf-8")).hexdigest()

        # 5. Store a replacement token while keeping the old token valid until email succeeds.
        new_data = {**raw_data, "resend_count": raw_data.get("resend_count", 0) + 1}
        new_pending_key = f"signup:pending:{new_token_hash}"

        try:
            await redis_utils.set_json_with_ttl(new_pending_key, new_data, ttl=600)
            await redis_utils.client.set(email_lookup_key, new_token_hash, ex=600)
        except Exception:
            logger.exception("Redis failure during resend")
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        # 6. Send new email
        email_sent = await send_verification_email(
            email=new_data["email"],
            owner_name=new_data["owner_name"],
            org_name=new_data["org_name"],
            raw_token=new_raw_token
        )

        if not email_sent:
            await redis_utils.delete_keys_safe([new_pending_key])
            await redis_utils.client.set(email_lookup_key, old_token_hash, ex=600)
            raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

        await redis_utils.delete_keys_safe([pending_key])

        return {"status": "success", "message": "Verification email resent."}

    async def login(self, data: LoginRequest) -> TokenResponse:
        """Secure login with credential verification and brute-force protection."""
        email = data.email.strip().lower()
        login_lock_key = f"login_lock:{email}"
        attempts_key = f"login_attempts:{email}"

        # 1. Check if account is locked
        redis_utils = get_redis_utils()
        if await redis_utils.client.get(login_lock_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Account locked due to multiple failed attempts. Try again later."
            )

        # 2. Get owner
        q = select(Owner).where(Owner.email == email)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()

        # 3. Verify credentials
        if not owner or not verify_password(data.password, owner.hashed_password):
            attempts = await redis_utils.client.incr(attempts_key)
            if attempts == 1:
                await redis_utils.client.expire(attempts_key, 600)

            if attempts >= 5:
                await redis_utils.client.setex(login_lock_key, 1800, "1")
                await redis_utils.client.delete(attempts_key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed attempts. Account locked for 30 minutes."
                )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid credentials. {5 - attempts} attempts remaining."
            )

        # 4. Check if verified
        if not owner.email_verified:
            raise HTTPException(status_code=403, detail="Email not verified")

        # 5. Successful login — reset attempts
        await redis_utils.client.delete(attempts_key)

        # 6. Issue tokens
        access_token = create_access_token(owner.id, owner.org_id, owner.email)
        refresh_token = create_refresh_token(owner.id)

        # 7. Store refresh token hash in DB
        rt_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        
        family = AuthSessionFamily(
            org_id=owner.org_id,
            user_id=owner.id
        )
        self.session.add(family)
        await self.session.flush()

        auth_session = AuthSession(
            user_id=owner.id,
            org_id=owner.org_id,
            token_family_id=family.id,
            refresh_token_hash=rt_hash,
            token_version_snapshot=1
        )
        self.session.add(auth_session)
        await self.session.commit()

        return TokenResponse(
            access_token=access_token, 
            refresh_token=refresh_token,
            onboarding_completed=owner.onboarding_completed
        )

    async def refresh_token(self, refresh_token: str) -> TokenResponse:
        """Implements refresh token rotation as per spec."""
        rt_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

        q = select(AuthSession).where(
            AuthSession.refresh_token_hash == rt_hash,
            AuthSession.revoked_at.is_(None)
        )
        result = await self.session.execute(q)
        db_rt = result.scalar_one_or_none()

        if not db_rt:
            # Check for token reuse
            q_any = select(AuthSession).where(AuthSession.refresh_token_hash == rt_hash)
            result_any = await self.session.execute(q_any)
            reused_session = result_any.scalar_one_or_none()
            if reused_session:
                q_family = select(AuthSessionFamily).where(AuthSessionFamily.id == reused_session.token_family_id)
                res_fam = await self.session.execute(q_family)
                family = res_fam.scalar_one()
                family.revoked_at = datetime.now(timezone.utc)
                reused_session.reuse_detected_at = datetime.now(timezone.utc)
                await self.session.commit()
                raise HTTPException(status_code=401, detail="Session compromised. Please login again.")
                
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

        q_family = select(AuthSessionFamily).where(AuthSessionFamily.id == db_rt.token_family_id)
        res_fam = await self.session.execute(q_family)
        family = res_fam.scalar_one()
        if family.revoked_at:
             raise HTTPException(status_code=401, detail="Session revoked")

        # Get owner data for new access token
        q_owner = select(Owner).where(Owner.id == db_rt.user_id)
        owner_result = await self.session.execute(q_owner)
        owner = owner_result.scalar_one_or_none()

        if not owner:
            raise HTTPException(status_code=401, detail="User not found")

        # Rotate tokens
        new_access = create_access_token(owner.id, owner.org_id, owner.email)
        new_refresh = create_refresh_token(owner.id)
        new_rt_hash = hashlib.sha256(new_refresh.encode("utf-8")).hexdigest()

        # FIX: same pattern — use begin_nested() since SELECT above opened a transaction
        async with self.session.begin_nested():
            db_rt.revoked_at = datetime.now(timezone.utc)
            
            new_db_rt = AuthSession(
                user_id=owner.id,
                org_id=owner.org_id,
                token_family_id=family.id,
                parent_session_id=db_rt.id,
                refresh_token_hash=new_rt_hash,
                token_version_snapshot=db_rt.token_version_snapshot + 1
            )
            self.session.add(new_db_rt)
            await self.session.flush()
            db_rt.replaced_by_session_id = new_db_rt.id

        await self.session.commit()

        return TokenResponse(
            access_token=new_access, 
            refresh_token=new_refresh,
            onboarding_completed=owner.onboarding_completed
        )
