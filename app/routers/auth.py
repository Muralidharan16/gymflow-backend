from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
import logging
import secrets
import hashlib
from ..config import settings
from ..database import get_db
from ..schemas.auth import LoginRequest, TokenResponse, RefreshRequest, RegisterRequest, RegisterResponse
from ..models.models import Staff, Organization, GymBranch, SaaSPlanTier, StaffRole, StaffSession, AuditLog
from ..middleware.auth_middleware import get_tenant_context
from ..schemas.tenant import TenantContext
from ..redis_client import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post('/register', response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    rate_limit_key = f"rate_limit:register:{client_ip}"

    # Strict rate limit: 5 per hour
    is_allowed = await rate_limit(rate_limit_key, limit=5, period_seconds=3600)
    if not is_allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many registration attempts.")

    email_clean = payload.email.strip().lower()

    # Check for duplicate email
    stmt = select(Staff).where(Staff.email == email_clean)
    result = await db.execute(stmt)
    if result.scalars().first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    now = datetime.now(timezone.utc)

    async with db.begin_nested() if db.in_transaction() else db.begin():
        # Create Org
        new_org = Organization(
            name=payload.org_name.strip(),
            pan_number=None, # Optional now
            plan_tier=SaaSPlanTier.basic,
            is_active=True
        )
        db.add(new_org)
        await db.flush()

        # Create Branch - Auto-generate first branch code
        new_branch = GymBranch(
            org_id=new_org.id,
            name=payload.branch_name.strip(),
            branch_code="BR001", # First branch sequence
            is_active=True
        )
        db.add(new_branch)
        await db.flush()

        # Create Staff (Owner) — BUG-1 fix: now stores owner_name
        hashed_password = pwd_context.hash(payload.password)
        new_owner = Staff(
            org_id=new_org.id,
            primary_branch_id=new_branch.id,
            role=StaffRole.owner,
            name=payload.owner_name.strip(),
            email=email_clean,
            password_hash=hashed_password,
            is_active=True
        )
        db.add(new_owner)
        await db.flush()

        # Generate tokens
        access_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

        access_token = create_access_token({
            "sub": str(new_owner.id),
            "org_id": str(new_org.id),
            "primary_branch_id": str(new_branch.id),
            "role": new_owner.role
        }, access_expires)

        raw_refresh_token = generate_refresh_token()

        new_session = StaffSession(
            staff_id=new_owner.id,
            refresh_token_hash=hash_refresh_token(raw_refresh_token),
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent", "unknown"),
            expires_at=now + refresh_expires
        )
        db.add(new_session)

        # Audit Log
        audit = AuditLog(
            org_id=new_org.id,
            actor_id=new_owner.id,
            action='org_registered',
            ip_address=client_ip,
            metadata_json={"branch_code": new_branch.branch_code}
        )
        db.add(audit)

    await db.commit()

    return RegisterResponse(
        access_token=access_token,
        refresh_token=raw_refresh_token,
        expires_at=now + access_expires,
        org_id=str(new_org.id),
        branch_id=str(new_branch.id),
        staff_id=str(new_owner.id)
    )


@router.post('/login', response_model=TokenResponse)
async def login(request: Request, payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    email_clean = payload.email.strip().lower()

    # Hybrid rate limit: IP + email
    ip_limit_key = f"rate_limit:login:ip:{client_ip}"
    email_limit_key = f"rate_limit:login:email:{email_clean}"

    ip_allowed = await rate_limit(ip_limit_key, limit=20, period_seconds=900)
    email_allowed = await rate_limit(email_limit_key, limit=5, period_seconds=900)

    if not ip_allowed or not email_allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts. Please try again later.")

    stmt = select(Staff).where(Staff.email == email_clean, Staff.deleted_at.is_(None))
    result = await db.execute(stmt)
    staff = result.scalar_one_or_none()

    if not staff or not pwd_context.verify(payload.password, staff.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not staff.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    now = datetime.now(timezone.utc)
    access_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    access_token = create_access_token({
        "sub": str(staff.id),
        "org_id": str(staff.org_id),
        "primary_branch_id": str(staff.primary_branch_id) if staff.primary_branch_id else None,
        "role": staff.role
    }, access_expires)

    raw_refresh_token = generate_refresh_token()

    new_session = StaffSession(
        staff_id=staff.id,
        refresh_token_hash=hash_refresh_token(raw_refresh_token),
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent", "unknown"),
        expires_at=now + refresh_expires
    )
    db.add(new_session)
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh_token,
        expires_at=now + access_expires
    )


@router.post('/refresh', response_model=TokenResponse)
async def refresh_token(request: Request, payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    hashed_token = hash_refresh_token(payload.refresh_token)

    stmt = select(StaffSession).where(StaffSession.refresh_token_hash == hashed_token)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if not session or session.is_revoked or session.expires_at < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    # Fetch staff
    staff_stmt = select(Staff).where(Staff.id == session.staff_id, Staff.deleted_at.is_(None))
    staff_res = await db.execute(staff_stmt)
    staff = staff_res.scalar_one_or_none()

    if not staff or not staff.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    # Rotate Token
    access_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    access_token = create_access_token({
        "sub": str(staff.id),
        "org_id": str(staff.org_id),
        "primary_branch_id": str(staff.primary_branch_id) if staff.primary_branch_id else None,
        "role": staff.role
    }, access_expires)

    new_raw_refresh = generate_refresh_token()

    # Invalidate old session
    session.is_revoked = True

    # Create new session
    new_session = StaffSession(
        staff_id=staff.id,
        refresh_token_hash=hash_refresh_token(new_raw_refresh),
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent", "unknown"),
        expires_at=now + refresh_expires
    )
    db.add(new_session)
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_raw_refresh,
        expires_at=now + access_expires
    )


@router.post('/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    """Revoke the current refresh token session.
    
    Security: Requires a valid access token (via get_tenant_context) AND the
    refresh token. This ensures only the session owner can revoke it.
    """
    hashed_token = hash_refresh_token(payload.refresh_token)

    stmt = select(StaffSession).where(
        StaffSession.refresh_token_hash == hashed_token,
        StaffSession.staff_id == context.staff_id,
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if session and not session.is_revoked:
        session.is_revoked = True
        await db.commit()
        logger.info(f"Session revoked for staff_id={context.staff_id}")

    return None
