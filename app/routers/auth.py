from fastapi import APIRouter, Depends, Request, Response as FastApiResponse, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_database import get_auth_db
from app.core.browser_session import (
    blacklist_access_token,
    clear_auth_cookies,
    clear_signup_poll_cookie,
    require_trusted_browser_origin,
    set_auth_cookies,
    set_signup_poll_cookie,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.auth import Owner
from app.schemas.auth import LoginRequest, SignupRequest, SignupStatusRequest
from app.services.auth_service import AuthService

import hashlib
import hmac
import json
import uuid


router = APIRouter(prefix="/auth", tags=["Authentication"])


def _no_store(response: FastApiResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _auth_error(status_code: int, detail: str) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    clear_auth_cookies(response)
    return response


@router.post("/signup")
async def signup(
    request: Request,
    response: FastApiResponse,
    data: SignupRequest,
    db: AsyncSession = Depends(get_auth_db),
):
    require_trusted_browser_origin(request)
    _no_store(response)

    service = AuthService(db)
    client_ip = request.client.host if request.client else "127.0.0.1"
    result = await service.signup(data, client_ip)

    poll_token = result.pop("signup_poll_token", None)
    if poll_token:
        from app.core.redis import get_redis_utils

        poll_token_hash = hashlib.sha256(poll_token.encode("utf-8")).hexdigest()
        email_hash = hashlib.sha256(data.email.strip().lower().encode("utf-8")).hexdigest()
        redis_utils = get_redis_utils()
        await redis_utils.client.setex(f"poll_token:{poll_token_hash}", 600, email_hash)
        set_signup_poll_cookie(response, poll_token)

    return result


@router.get("/verify")
async def verify(token: str, db: AsyncSession = Depends(get_auth_db)):
    """Verify account ownership without establishing a browser login on a GET."""
    service = AuthService(db)
    result = await service.verify(token)

    if "error" in result:
        reason = result["error"]
        response = RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/verify-failed?reason={reason}")
        response.headers["Cache-Control"] = "no-store"
        return response

    owner = result["owner"]
    org = result["org"]

    from app.core.redis import get_redis_utils

    redis_utils = get_redis_utils()
    signup_poll_token_hash = result.get("signup_poll_token_hash")
    email_hash = hashlib.sha256(owner.email.strip().lower().encode("utf-8")).hexdigest()
    sync_data = {
        "onboarding_completed": owner.onboarding_completed,
        "sub": str(owner.id),
        "name": owner.owner_name,
        "organizationName": org.name if org else "Studio Owner",
        "signup_poll_token_hash": signup_poll_token_hash,
    }
    await redis_utils.client.setex(
        f"signup:sync:{email_hash}",
        300,
        json.dumps(sync_data),
    )

    if signup_poll_token_hash:
        await redis_utils.client.setex(
            f"poll_token:{signup_poll_token_hash}",
            600,
            email_hash,
        )

    response = RedirectResponse(url=f"{settings.FRONTEND_URL}/auth/verify-success")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/signup-status")
async def get_signup_status(
    data: SignupStatusRequest,
    response: FastApiResponse,
    request: Request,
    db: AsyncSession = Depends(get_auth_db),
):
    from app.core.redis import get_redis_utils

    require_trusted_browser_origin(request)
    _no_store(response)

    redis_utils = get_redis_utils()
    email = data.email.strip().lower()
    poll_token = request.cookies.get("signup_poll_token")
    if not poll_token:
        raise HTTPException(status_code=403, detail="Signup session unavailable")

    client_ip = request.client.host if request.client else "127.0.0.1"
    rate_key = f"ratelimit:signup_status:{client_ip}"
    if await redis_utils.is_rate_limited(rate_key, limit=30, ttl=60):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    poll_token_hash = hashlib.sha256(poll_token.encode("utf-8")).hexdigest()
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()

    poll_token_email_hash = await redis_utils.client.get(f"poll_token:{poll_token_hash}")
    if not poll_token_email_hash or not hmac.compare_digest(poll_token_email_hash, email_hash):
        raise HTTPException(status_code=403, detail="Invalid signup session")

    sync_key = f"signup:sync:{email_hash}"
    sync_data_raw = await redis_utils.client.get(sync_key)
    if sync_data_raw:
        sync_data = json.loads(sync_data_raw)
        expected_hash = sync_data.get("signup_poll_token_hash")
        if not expected_hash or not hmac.compare_digest(expected_hash, poll_token_hash):
            raise HTTPException(status_code=403, detail="Invalid signup session")

        try:
            owner_id = uuid.UUID(str(sync_data.get("sub")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Verified account unavailable") from exc

        owner_result = await db.execute(select(Owner).where(Owner.id == owner_id))
        owner = owner_result.scalar_one_or_none()
        if owner is None or owner.email.strip().lower() != email:
            raise HTTPException(status_code=401, detail="Verified account unavailable")

        tokens = await AuthService(db).issue_session(owner)
        set_auth_cookies(response, tokens)
        clear_signup_poll_cookie(response)
        await redis_utils.delete_keys_safe(
            [
                sync_key,
                f"poll_token:{poll_token_hash}",
            ]
        )

        return {
            "status": "verified",
            "onboarding_completed": owner.onboarding_completed,
            "user": {
                "email": owner.email,
                "id": str(owner.id),
                "name": owner.owner_name,
                "organizationName": sync_data.get("organizationName"),
            },
        }

    pending_token_hash = await redis_utils.client.get(f"signup:email:{email_hash}")
    if not pending_token_hash:
        return {"status": "pending"}

    pending_data = await redis_utils.get_json(f"signup:pending:{pending_token_hash}")
    if not pending_data:
        return {"status": "pending"}

    if not AuthService.verify_signup_poll_token(pending_data, poll_token):
        raise HTTPException(status_code=403, detail="Invalid signup session")

    return {"status": "pending"}


@router.post("/resend-verification")
async def resend_verification(
    request: Request,
    response: FastApiResponse,
    data: dict,
    db: AsyncSession = Depends(get_auth_db),
):
    require_trusted_browser_origin(request)
    _no_store(response)
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=422, detail="Email is required")
    return await AuthService(db).resend_verification(email)


@router.post("/login")
async def login(
    request: Request,
    response: FastApiResponse,
    data: LoginRequest,
    db: AsyncSession = Depends(get_auth_db),
):
    require_trusted_browser_origin(request)
    service = AuthService(db)
    tokens = await service.login(data)
    email = data.email.strip().lower()

    result = await db.execute(select(Owner).where(Owner.email == email))
    owner = result.scalar_one()

    from app.models.organization import Organization

    org_result = await db.execute(select(Organization).where(Organization.id == owner.org_id))
    org = org_result.scalar_one_or_none()
    org_name = org.name if org else "Studio Owner"

    set_auth_cookies(response, tokens)
    clear_signup_poll_cookie(response)
    return {
        "user": {
            "id": str(owner.id),
            "email": owner.email,
            "name": owner.owner_name,
            "organizationName": org_name,
        },
        "onboarding_completed": owner.onboarding_completed,
    }


@router.post("/refresh")
async def refresh(
    request: Request,
    response: FastApiResponse,
    db: AsyncSession = Depends(get_auth_db),
):
    require_trusted_browser_origin(request)
    raw_refresh_token = request.cookies.get("refresh_token")
    if not raw_refresh_token:
        return _auth_error(401, "Refresh cookie missing")

    try:
        tokens = await AuthService(db).refresh_token(raw_refresh_token)
    except HTTPException as exc:
        return _auth_error(exc.status_code, str(exc.detail))

    set_auth_cookies(response, tokens)
    return {
        "status": "success",
        "onboarding_completed": tokens.onboarding_completed,
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: FastApiResponse,
    db: AsyncSession = Depends(get_auth_db),
):
    require_trusted_browser_origin(request)
    service = AuthService(db)

    raw_refresh_token = request.cookies.get("refresh_token")
    raw_access_token = request.cookies.get("access_token")
    revoked_family = await service.revoke_refresh_family(raw_refresh_token)
    try:
        await blacklist_access_token(raw_access_token)
    except Exception:
        if revoked_family is None:
            raise HTTPException(status_code=503, detail="Logout validation unavailable")

    clear_auth_cookies(response)
    clear_signup_poll_cookie(response)
    return {"status": "success"}


from app.core.deps import get_current_active_staff, Staff


@router.get("/me")
async def get_me(
    response: FastApiResponse,
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db),
):
    _no_store(response)
    result = await db.execute(select(Owner).where(Owner.id == current_staff.id))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner not found")

    from app.models.organization import Organization

    org_result = await db.execute(select(Organization).where(Organization.id == owner.org_id))
    org = org_result.scalar_one_or_none()
    org_name = org.name if org else "Studio Owner"

    return {
        "user": {
            "id": str(owner.id),
            "email": owner.email,
            "name": owner.owner_name,
            "organizationName": org_name,
        },
        "onboarding_completed": owner.onboarding_completed,
    }
