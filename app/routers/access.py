from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..routers.devices import verify_device
from ..models.models import Device, GymBranch
from ..schemas.attendance import AccessScanRequest, AccessScanResponse
from ..services.attendance import process_attendance_scan
from ..redis_client import rate_limit
from sqlalchemy import select

router = APIRouter(prefix="/access", tags=["access"])


@router.post('/scan', response_model=AccessScanResponse)
async def scan_fingerprint(
    payload: AccessScanRequest,
    device: Device = Depends(verify_device),
    db: AsyncSession = Depends(get_db),
):
    """
    Hardware fingerprint scan endpoint.
    
    Security: Device authenticates via X-Device-UID + X-Api-Key headers.
    The branch_id and org_id are derived from the device, NOT from the request body.
    This prevents spoofing — hardware cannot claim to be at a different branch.
    """
    # Rate limit per device to prevent abuse (generous: 120/min for rush hour)
    key = f"rate:access:{device.device_uid}"
    allowed = await rate_limit(key, limit=120, period_seconds=60)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    # Derive org_id from device's branch
    branch_stmt = select(GymBranch.org_id).where(GymBranch.id == device.branch_id)
    branch_res = await db.execute(branch_stmt)
    org_id = branch_res.scalar_one()

    result = await process_attendance_scan(
        db=db,
        org_id=str(org_id),
        branch_id=str(device.branch_id),
        fingerprint_id=payload.fingerprint_id,
        device_id=str(device.id),
    )

    return AccessScanResponse(
        access_granted=result["access_granted"],
        reason=result["reason"],
        member_name=result.get("member_name"),
        member_uid=result.get("member_uid"),
        subscription_end=result.get("subscription_end"),
    )
