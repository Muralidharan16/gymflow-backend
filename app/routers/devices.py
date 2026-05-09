from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timezone
import secrets
import hashlib
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..models.models import Device, Member, GymBranch, StaffRole, DeviceStatus
from ..schemas.device import DeviceRegisterRequest, DeviceRegisterResponse, DeviceStatusResponse

router = APIRouter(prefix="/devices", tags=["devices"])

def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()

async def verify_device(
    x_device_uid: str = Header(..., description="Unique Device Hardware ID"),
    x_api_key: str = Header(..., description="Device API Key"),
    db: AsyncSession = Depends(get_db)
) -> Device:
    """Authenticate a hardware device via UID + API key.
    
    Does NOT commit — the calling router is responsible for committing.
    This prevents double-commit issues and preserves transaction safety.
    """
    hashed_key = hash_api_key(x_api_key)
    
    stmt = select(Device).where(
        Device.device_uid == x_device_uid,
        Device.deleted_at.is_(None)
    )
    result = await db.execute(stmt)
    device = result.scalar_one_or_none()
    
    if not device or device.api_key_hash != hashed_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device credentials")
    
    # Mark online — commit is deferred to the calling router
    device.status = DeviceStatus.online
    device.last_seen_at = datetime.now(timezone.utc)
        
    return device


@router.post('/register', response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest, 
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db)
):
    # Ensure device is not already registered
    check_stmt = select(Device).where(Device.device_uid == payload.device_uid)
    check_res = await db.execute(check_stmt)
    if check_res.scalars().first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device UID already registered")

    # BUG-2 fix: Validate branch belongs to this org (prevents cross-tenant device binding)
    branch_stmt = select(GymBranch).where(
        GymBranch.id == payload.branch_id,
        GymBranch.org_id == context.org_id,
        GymBranch.deleted_at.is_(None),
    )
    branch_res = await db.execute(branch_stmt)
    if not branch_res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid branch for this organization")

    raw_api_key = secrets.token_urlsafe(32)
    
    new_device = Device(
        org_id=context.org_id,  # BUG-2 fix: org_id is required (NOT NULL FK)
        branch_id=payload.branch_id,
        device_uid=payload.device_uid,
        api_key_hash=hash_api_key(raw_api_key),
        status=DeviceStatus.offline,
        firmware_version=payload.firmware_version
    )
    
    db.add(new_device)
    await db.commit()
    await db.refresh(new_device)
    
    return DeviceRegisterResponse(id=new_device.id, api_key=raw_api_key)


@router.get('/status', response_model=list[DeviceStatusResponse])
async def devices_status(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db)
):
    # Needs to join branch to ensure it belongs to the org
    # Since branch_id isn't directly on staff unless primary, we can look up by primary_branch
    # For Phase 1 we will allow staff to see devices in their primary branch.
    
    if not context.primary_branch_id:
        return []
        
    stmt = select(Device).where(
        Device.org_id == context.org_id,  # BUG-6 fix: explicit tenant isolation
        Device.branch_id == context.primary_branch_id,
        Device.deleted_at.is_(None)
    )
    q = await db.execute(stmt)
    devices = q.scalars().all()
    
    return [
        DeviceStatusResponse(
            id=d.id,
            branch_id=d.branch_id,
            device_uid=d.device_uid,
            status=d.status,
            last_seen_at=d.last_seen_at,
            firmware_version=d.firmware_version
        ) for d in devices
    ]


@router.post('/ping')
async def device_heartbeat(device: Device = Depends(verify_device), db: AsyncSession = Depends(get_db)):
    """Hardware heartbeat endpoint to update last_seen_at."""
    device.last_seen_at = datetime.now(timezone.utc)
    device.status = DeviceStatus.online
    await db.commit()
    return {"status": "ok"}


@router.post('/sync-members')
async def sync_members(device: Device = Depends(verify_device), db: AsyncSession = Depends(get_db)):
    """Device pulls active members for offline fingerprint caching."""
    # Return members who belong to this branch's org and have a fingerprint_id
    # We need to join with GymBranch to get the org_id
    from ..models.models import GymBranch
    
    branch_stmt = select(GymBranch).where(GymBranch.id == device.branch_id)
    branch_res = await db.execute(branch_stmt)
    branch = branch_res.scalar_one()
    
    q = await db.execute(select(Member).where(
        Member.org_id == branch.org_id, 
        Member.fingerprint_id.isnot(None),
        Member.deleted_at.is_(None)
    ))
    members = q.scalars().all()
    
    items = []
    for m in members:
        items.append({
            'member_id': str(m.id),
            'name': m.name,
            'fingerprint_id': m.fingerprint_id,
            'uid': m.member_uid
        })
        
    return { 'members': items }
