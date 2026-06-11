from fastapi import APIRouter, Depends, status, Request, Response as FastApiResponse, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.auth import Owner
from app.core.database import get_db
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, RefreshRequest
from app.schemas.common import Response
from app.services.auth_service import AuthService
from app.core.config import settings
import hashlib
import hmac


router = APIRouter(prefix="/auth", tags=["Authentication"])

def set_auth_cookies(response: FastApiResponse, tokens: TokenResponse):
    # Determine if we are in production
    is_prod = settings.ENVIRONMENT == "production"
    
    response.set_cookie(
        key="access_token",
        value=tokens.access_token,
        httponly=True,
        secure=is_prod, # False for local dev
        samesite="lax",
        max_age=15 * 60
    )
    response.set_cookie(
        key="refresh_token",
        value=tokens.refresh_token,
        httponly=True,
        secure=is_prod, # False for local dev
        samesite="lax",
        max_age=7 * 24 * 60 * 60
    )

@router.post("/signup")
async def signup(request: Request, data: SignupRequest, db: AsyncSession = Depends(get_db)):
    service = AuthService(db)
    client_ip = request.client.host if request.client else "127.0.0.1"
    result = await service.signup(data, client_ip)

    poll_token = result.get("signup_poll_token")
    if poll_token:
        from app.core.redis import get_redis_utils
        poll_token_hash = hashlib.sha256(poll_token.encode("utf-8")).hexdigest()
        email_hash = hashlib.sha256(data.email.strip().lower().encode("utf-8")).hexdigest()
        redis_utils = get_redis_utils()
        await redis_utils.client.setex(f"poll_token:{poll_token_hash}", 600, email_hash)

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
    from app.models.auth_session import AuthSession, AuthSessionFamily

    access_token = create_access_token(owner.id, org.id, owner.email)
    refresh_token = create_refresh_token(owner.id)
    
    # Store refresh token
    rt_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    
    family = AuthSessionFamily(
        org_id=org.id,
        user_id=owner.id
    )
    db.add(family)
    await db.flush()

    db_rt = AuthSession(
        user_id=owner.id,
        org_id=org.id,
        token_family_id=family.id,
        refresh_token_hash=rt_hash,
        token_version_snapshot=1
    )
    db.add(db_rt)
    await db.commit()
    
    # --- CROSS-DEVICE SYNC ---
    # Store tokens in Redis so the Laptop can "pick them up"
    from app.core.redis import get_redis_utils
    import json
    redis_utils = get_redis_utils()
    signup_poll_token_hash = result.get("signup_poll_token_hash")
    sync_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "onboarding_completed": owner.onboarding_completed,
        "sub": str(owner.id),
        "name": owner.owner_name,
        "organizationName": org.name if org else "Studio Owner",
        "signup_poll_token_hash": signup_poll_token_hash,
    }
    await redis_utils.client.setex(f"signup:sync:{owner.email}", 300, json.dumps(sync_data))

    if signup_poll_token_hash:
        email_hash = hashlib.sha256(owner.email.strip().lower().encode("utf-8")).hexdigest()
        await redis_utils.client.setex(f"poll_token:{signup_poll_token_hash}", 600, email_hash)
    
    # Redirect to a simple success message page instead of onboarding
    response = RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/verify-success")
    set_auth_cookies(response, TokenResponse(
        access_token=access_token, 
        refresh_token=refresh_token,
        onboarding_completed=owner.onboarding_completed
    ))
    return response

@router.get("/signup-status")
async def get_signup_status(email: str, poll_token: str, response: FastApiResponse, request: Request):
    """
    Endpoint for the same device to poll signup verification status.

    SECURITY: Email alone is never sufficient. The poll_token is validated via a
    reverse Redis mapping before any status is returned. Wrong/missing/expired
    poll_token always returns 403.
    """
    from app.core.redis import get_redis_utils
    import json
    redis_utils = get_redis_utils()
    email = email.strip().lower()

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"

    if not poll_token:
        raise HTTPException(status_code=403, detail="Invalid signup status token")

    client_ip = request.client.host if request.client else "127.0.0.1"
    rate_key = f"ratelimit:signup_status:{client_ip}"
    if await redis_utils.is_rate_limited(rate_key, limit=30, ttl=60):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    poll_token_hash = hashlib.sha256(poll_token.encode("utf-8")).hexdigest()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()

    poll_token_email_hash = await redis_utils.client.get(f"poll_token:{poll_token_hash}")
    if not poll_token_email_hash or not hmac.compare_digest(poll_token_email_hash, email_hash):
        raise HTTPException(status_code=403, detail="Invalid signup status token")

    sync_data_raw = await redis_utils.client.get(f"signup:sync:{email}")
    if sync_data_raw:
        sync_data = json.loads(sync_data_raw)
        expected_hash = sync_data.get("signup_poll_token_hash")
        if not expected_hash or not hmac.compare_digest(expected_hash, poll_token_hash):
            raise HTTPException(status_code=403, detail="Invalid signup status token")

        set_auth_cookies(response, TokenResponse(
            access_token=sync_data["access_token"],
            refresh_token=sync_data["refresh_token"],
            onboarding_completed=sync_data["onboarding_completed"]
        ))

        await redis_utils.client.delete(f"signup:sync:{email}")

        pending_token_hash = await redis_utils.client.get(f"signup:email:{email_hash}")
        if pending_token_hash:
            await redis_utils.delete_keys_safe([
                f"signup:pending:{pending_token_hash}",
                f"signup:email:{email_hash}"
            ])

        return {
            "status": "verified",
            "onboarding_completed": sync_data["onboarding_completed"],
            "access_token": sync_data["access_token"],
            "refresh_token": sync_data["refresh_token"],
            "user": {
                "email": email,
                "id": sync_data.get("sub"),
                "name": sync_data.get("name"),
                "organizationName": sync_data.get("organizationName")
            }
        }

    pending_token_hash = await redis_utils.client.get(f"signup:email:{email_hash}")
    if not pending_token_hash:
        return {"status": "pending"}

    pending_data = await redis_utils.get_json(f"signup:pending:{pending_token_hash}")
    if not pending_data:
        return {"status": "pending"}

    if not AuthService.verify_signup_poll_token(pending_data, poll_token):
        raise HTTPException(status_code=403, detail="Invalid signup status token")

    return {"status": "pending"}

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
    email = data.email.strip().lower()
    
    # Get user details for response
    result = await db.execute(select(Owner).where(Owner.email == email))
    owner = result.scalar_one()

    from app.models.organization import Organization
    org_result = await db.execute(select(Organization).where(Organization.id == owner.org_id))
    org = org_result.scalar_one_or_none()
    org_name = org.name if org else "Studio Owner"

    set_auth_cookies(response, tokens)
    
    return {
        "user": {
            "id": str(owner.id),
            "email": owner.email,
            "name": owner.owner_name,
            "organizationName": org_name
        },
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "onboarding_completed": owner.onboarding_completed
    }

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
    return {
        "status": "success",
        "message": "Token refreshed",
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "onboarding_completed": tokens.onboarding_completed,
    }

from app.core.deps import get_current_active_staff, Staff

@router.get("/me")
async def get_me(
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    # Fetch owner details
    result = await db.execute(select(Owner).where(Owner.id == current_staff.id))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner not found")
        
    from app.models.organization import Organization
    org_result = await db.execute(select(Organization).where(Organization.id == owner.org_id))
    org = org_result.scalar_one_or_none()
    org_name = org.name if org else "Studio Owner"
    
    return {
        "id": str(owner.id),
        "email": owner.email,
        "name": owner.owner_name,
        "organizationName": org_name
    }
