from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.core.deps import Staff
from app.models.attendance import CheckInMethod
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.attendance import AttendanceResponse, CheckInRequest
from app.services.attendance_service import AttendanceService
from app.core.exceptions import NotFoundError, ValidationError, SubscriptionNotActive

router = APIRouter(tags=["Attendance"])


# Public endpoint - no authentication required
@router.get("/check-access/{uid}")
async def check_access(
    uid: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Public endpoint for QR code access check.
    Does NOT require JWT authentication.
    Returns attendance log with access status.
    """
    service = AttendanceService(db)
    try:
        log = await service.check_access(uid)
        await db.commit()
        return Response(data=AttendanceResponse.model_validate(log))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except SubscriptionNotActive as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": str(e), "error_code": "SUBSCRIPTION_NOT_ACTIVE"}
        )


@router.post("/gyms/{gym_id}/attendance", response_model=Response[AttendanceResponse])
async def manual_checkin(
    gym_id: UUID,
    data: CheckInRequest,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Manual check-in by staff for a member.
    """
    service = AttendanceService(db)
    try:
        log = await service.manual_checkin(
            gym_id=gym_id,
            member_id=data.member_id,
            method=CheckInMethod.MANUAL,
            staff_id=current_staff.id
        )
        await db.commit()
        return Response(data=AttendanceResponse.model_validate(log))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except SubscriptionNotActive as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": str(e), "error_code": "SUBSCRIPTION_NOT_ACTIVE"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.post("/gyms/{gym_id}/attendance/{log_id}/checkout", response_model=Response[AttendanceResponse])
async def checkout(
    gym_id: UUID,
    log_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Record check-out time for an attendance log.
    """
    service = AttendanceService(db)
    try:
        log = await service.checkout(gym_id, log_id)
        await db.commit()
        return Response(data=AttendanceResponse.model_validate(log))
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


@router.get("/gyms/{gym_id}/attendance", response_model=PaginatedResponse[AttendanceResponse])
async def list_attendance_logs(
    gym_id: UUID,
    date_filter: Optional[date] = Query(None, description="Filter by date (YYYY-MM-DD)"),
    member_id: Optional[UUID] = Query(None, description="Filter by member ID"),
    granted: Optional[bool] = Query(None, description="Filter by access granted"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    List attendance logs with pagination and filters.
    """
    service = AttendanceService(db)
    logs, total = await service.list_logs(
        gym_id=gym_id,
        date_filter=date_filter,
        member_id=member_id,
        granted=granted,
        page=page,
        size=size
    )
    return PaginatedResponse(
        data=[AttendanceResponse.model_validate(log) for log in logs],
        page=page,
        size=size,
        total=total
    )


@router.get("/members/{member_id}/attendance", response_model=PaginatedResponse[AttendanceResponse])
async def member_attendance_history(
    member_id: UUID,
    gym_id: UUID = Query(..., description="Gym ID for scoping"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get attendance history for a specific member (paginated).
    """
    service = AttendanceService(db)
    try:
        logs, total = await service.member_history(
            member_id=member_id,
            gym_id=gym_id,
            page=page,
            size=size
        )
        return PaginatedResponse(
            data=[AttendanceResponse.model_validate(log) for log in logs],
            page=page,
            size=size,
            total=total
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )