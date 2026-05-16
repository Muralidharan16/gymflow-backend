# FIXED: [FIX 5] Standardized pagination params (page, page_size) on member list route.
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.core.deps import Staff
from app.models.member import MemberStatus
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.member import MemberResponse, MemberCreate, MemberUpdate, MeasurementResponse, MeasurementCreate
from app.services.member_service import MemberService
from app.core.exceptions import NotFoundError, ValidationError, MemberLimitExceeded

router = APIRouter(prefix="/gyms/{gym_id}/members", tags=["Members"])


@router.get("", response_model=PaginatedResponse[MemberResponse])
async def list_members(
    gym_id: UUID,
    status: Optional[MemberStatus] = Query(None, description="Filter by member status"),
    search: Optional[str] = Query(None, description="Search by name or phone"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    List members in a gym with pagination and filtering.
    """
    service = MemberService(db)
    members, total = await service.list_members(
        gym_id=gym_id,
        status=status,
        search_term=search,
        page=page,
        size=page_size
    )
    from math import ceil
    return PaginatedResponse(
        data=[MemberResponse.model_validate(m) for m in members],
        page=page,
        size=page_size,
        total=total,
        pages=ceil(total / page_size) if page_size else 0,
    )


@router.post("", response_model=Response[MemberResponse])
async def create_member(
    gym_id: UUID,
    data: MemberCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new member in the gym.
    """
    service = MemberService(db)
    try:
        member = await service.create_member(gym_id, data, current_staff.id)
        await db.commit()
        return Response(data=MemberResponse.model_validate(member))
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )
    except MemberLimitExceeded as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": "MEMBER_LIMIT_EXCEEDED"}
        )


@router.get("/{member_id}", response_model=Response[MemberResponse])
async def get_member_profile(
    gym_id: UUID,
    member_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get full member profile.
    """
    service = MemberService(db)
    try:
        member = await service.get_member(member_id, gym_id)
        return Response(data=MemberResponse.model_validate(member))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.put("/{member_id}", response_model=Response[MemberResponse])
async def update_member(
    gym_id: UUID,
    member_id: UUID,
    data: MemberUpdate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Update member details (name, phone, email, address, gender, notes, status).
    """
    service = MemberService(db)
    try:
        member = await service.update_member(gym_id, member_id, data, current_staff.id)
        await db.commit()
        return Response(data=MemberResponse.model_validate(member))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.delete("/{member_id}", response_model=Response[MessageResponse])
async def delete_member(
    gym_id: UUID,
    member_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Soft delete a member (set is_active=False).
    """
    service = MemberService(db)
    try:
        await service.soft_delete(gym_id, member_id)
        await db.commit()
        return Response(data=MessageResponse(message="Member deleted successfully"))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.get("/{member_id}/measurements", response_model=Response[list[MeasurementResponse]])
async def get_member_measurements(
    gym_id: UUID,
    member_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get measurement history for a member, ordered by measured_on DESC.
    """
    service = MemberService(db)
    try:
        measurements = await service.get_measurements(member_id, gym_id)
        return Response(data=[MeasurementResponse.model_validate(m) for m in measurements])
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.post("/{member_id}/measurements", response_model=Response[MeasurementResponse])
async def log_measurement(
    gym_id: UUID,
    member_id: UUID,
    data: MeasurementCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Log a new measurement for a member.
    """
    service = MemberService(db)
    try:
        measurement = await service.log_measurement(member_id, gym_id, data, current_staff.id)
        await db.commit()
        return Response(data=MeasurementResponse.model_validate(measurement))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


# Separate endpoint for QR code (no gym_id in path, uses member_uid)
@router.get("/qr/{member_uid}", tags=["Members"])
async def get_member_qr_code(
    member_uid: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns QR code PNG image for a member.
    No authentication required (public access for scanning).
    """
    service = MemberService(db)
    try:
        png_bytes = await service.get_member_qr_png(member_uid)
        return Response(content=png_bytes, media_type="image/png")
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )