import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timezone

from app.core.database import get_db
from app.core.deps import get_current_active_staff, Staff
from app.schemas.address import (
    PublicAddressSchema, PrivateAddressSchema, CreateAddressSchema,
    PublicMemberAddressSchema, PrivateMemberAddressSchema, UpdateAddressMapsSchema
)
from app.models.address import OrganizationAddress, MemberAddress
from app.services.address_service import set_primary_address

router = APIRouter(prefix="/addresses", tags=["Addresses"])
org_address_router = APIRouter(tags=["Organization Addresses"])
member_address_router = APIRouter(tags=["Member Addresses"])

async def require_org_admin(current_user: Staff = Depends(get_current_active_staff)) -> Staff:
    """
    Async RBAC dependency ensuring only org_admin, owner, or superadmin can access private fields.
    """
    if current_user.role not in ("org_admin", "owner", "superadmin", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation forbidden. Org administrator privileges required."
        )
    return current_user

# =====================================================================
# SECURITY COMPLIANCE POLICY: ENDPOINT EXPOSURE LIMITS
# =====================================================================
@router.get("/", response_model=List[PublicAddressSchema])
async def list_public_addresses(
    response: Response,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(get_current_active_staff)
) -> List[PublicAddressSchema]:
    """
    Returns a public paginated list of active organization addresses. 
    """
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"
    stmt = (
        select(OrganizationAddress)
        .where(OrganizationAddress.deleted_at.is_(None))
        .where(OrganizationAddress.effective_until.is_(None))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    records = result.scalars().all()
    
    return [PublicAddressSchema.serialize_from_db(rec) for rec in records]

@router.get("/{address_id}", response_model=PublicAddressSchema)
async def get_public_address(
    address_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(get_current_active_staff)
) -> PublicAddressSchema:
    """
    Returns the public representation of an address (no PII fields like address_line1, postal_code).
    """
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"
    result = await db.get(OrganizationAddress, address_id)
    if not result or result.deleted_at is not None or result.effective_until is not None:
        raise HTTPException(status_code=404, detail="Address not found.")
    return PublicAddressSchema.serialize_from_db(result)

@router.get("/{address_id}/private", response_model=PrivateAddressSchema)
async def get_private_address(
    address_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(require_org_admin)
) -> OrganizationAddress:
    """
    Guarded route returning the PrivateAddressSchema.
    """
    result = await db.get(OrganizationAddress, address_id)
    if not result or result.deleted_at is not None or result.effective_until is not None:
        raise HTTPException(status_code=404, detail="Address not found.")
    return result


@router.patch("/{address_id}/maps", response_model=PrivateAddressSchema)
async def update_address_maps(
    address_id: uuid.UUID,
    payload: UpdateAddressMapsSchema,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(require_org_admin)
) -> OrganizationAddress:
    """
    Exposes the endpoint to patch address maps config (google_place_id and maps_embed_allowed).
    Enforces strict RBAC: only org admins/owners can call this.
    Triggers geocoding background task if place_id changes or is newly set.
    """
    from app.services.maps_service import MapsVerificationStatus, MapsVerificationSource
    from app.tasks.geocoding import geocode_address_task

    result = await db.execute(
        select(OrganizationAddress)
        .where(OrganizationAddress.id == address_id)
        .where(OrganizationAddress.deleted_at.is_(None))
        .where(OrganizationAddress.effective_until.is_(None))
    )
    addr = result.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Active address not found")

    place_id_changed = False
    if payload.google_place_id is not None:
        new_place_id = payload.google_place_id.strip() if payload.google_place_id.strip() else None
        if addr.google_place_id != new_place_id:
            addr.google_place_id = new_place_id
            place_id_changed = True

    if payload.maps_embed_allowed is not None:
        addr.maps_embed_allowed = payload.maps_embed_allowed

    if place_id_changed:
        addr.maps_retry_count = 0
        addr.maps_next_retry_at = None
        addr.maps_verification_error = None
        addr.maps_verification_source = MapsVerificationSource.MANUAL_OVERRIDE.value
        addr.maps_verification_status = MapsVerificationStatus.pending.value
        addr.latitude = None
        addr.longitude = None
        addr.is_verified = False

    await db.commit()
    await db.refresh(addr)
    
    if place_id_changed:
        geocode_address_task.delay(str(address_id))

    return addr

@router.patch("/{address_id}", response_model=PrivateAddressSchema)
async def update_address(
    address_id: uuid.UUID,
    payload: CreateAddressSchema,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(require_org_admin)
) -> OrganizationAddress:
    """
    Temporal append-only update endpoint. Closes the previous address life
    and spawns a transactionally integrated new record that fires audit listeners.
    """
    result = await db.execute(
        select(OrganizationAddress)
        .where(OrganizationAddress.id == address_id)
        .where(OrganizationAddress.effective_until.is_(None))
    )
    addr = result.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Active address not found")

    # End the historical record
    addr.effective_until = datetime.now(timezone.utc)

    # Spawn new record inheriting structural state
    new_addr = OrganizationAddress(
        org_id=addr.org_id,
        address_type=payload.address_type,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        state_province=payload.state_province,
        postal_code=payload.postal_code,
        country_code=payload.country_code,
        label=payload.label,
        is_primary=addr.is_primary,
        effective_from=datetime.now(timezone.utc)
    )

    # Inject session context variables for audit hooks
    new_addr._changed_by = current_user.id
    new_addr._ip_address = request.client.host if request.client else "127.0.0.1"

    db.add(new_addr)
    await db.commit()
    await db.refresh(new_addr)
    return new_addr

# =====================================================================
# PRIMARY ROUTE SETTER
# =====================================================================
@org_address_router.patch("/organizations/{org_id}/addresses/{address_id}/set-primary", response_model=PrivateAddressSchema)
async def set_primary_org_address(
    org_id: uuid.UUID,
    address_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(require_org_admin)
) -> OrganizationAddress:
    """
    Protected endpoint setting the organization's primary address.
    """
    target = await set_primary_address(address_id, org_id, db)
    await db.commit()
    await db.refresh(target)
    return target

# =====================================================================
# MEMBER ADDRESS ENDPOINTS
# =====================================================================
@member_address_router.get("/members/{member_id}/addresses", response_model=List[PublicMemberAddressSchema])
async def list_member_addresses(
    member_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(get_current_active_staff)
) -> List[PublicMemberAddressSchema]:
    """
    Lists member addresses strictly using the PublicMemberAddressSchema,
    blocking coordinates, postal code, and street details.
    """
    stmt = (
        select(MemberAddress)
        .where(MemberAddress.member_id == member_id)
        .where(MemberAddress.deleted_at.is_(None))
    )
    res = await db.execute(stmt)
    records = res.scalars().all()
    return records

@member_address_router.get("/members/{member_id}/addresses/{address_id}/private", response_model=PrivateMemberAddressSchema)
async def get_private_member_address(
    member_id: uuid.UUID,
    address_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Staff = Depends(require_org_admin)
) -> MemberAddress:
    """
    Guarded administrative retrieval of member details, allowing coordinate reads.
    """
    stmt = (
        select(MemberAddress)
        .where(MemberAddress.id == address_id)
        .where(MemberAddress.member_id == member_id)
        .where(MemberAddress.deleted_at.is_(None))
    )
    res = await db.execute(stmt)
    addr = res.scalar_one_or_none()
    if not addr:
        raise HTTPException(status_code=404, detail="Member address not found")
    return addr
