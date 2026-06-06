from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, text, delete
import uuid
from typing import List
from datetime import datetime, timezone

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_org_admin, require_branch_staff_role, Staff
from app.schemas.branch_operating_hours import (
    BranchHoursProjectionResponse,
    BulkOperatingHoursRequest,
    BulkSpecialHoursRequest,
    BranchOperatingHoursResponse,
    BranchSpecialHoursResponse,
    OrganizationOperatingHoursResponse
)
from app.models.branch_operating_hours import (
    BranchOperatingHours,
    OrganizationOperatingHours,
    BranchSpecialHours,
    BranchHoursProjection
)

router = APIRouter(tags=["operating-hours"])

@router.get("/branches/{branch_id}/hours/projection", response_model=BranchHoursProjectionResponse)
async def get_branch_hours_projection(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns the computed and cached CQRS projection of the branch's operating hours.
    This is the primary endpoint for public clients/members to view schedules.
    """
    stmt = select(BranchHoursProjection).where(BranchHoursProjection.branch_id == branch_id)
    projection = await db.scalar(stmt)
    
    if not projection:
        raise HTTPException(status_code=404, detail="Projection not found for this branch. Has not been configured yet.")
        
    return projection


@router.get("/branches/{branch_id}/hours", response_model=List[BranchOperatingHoursResponse])
async def get_branch_operating_hours(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff)
):
    """
    Returns the standard weekly operating hours for a specific branch.
    Includes active (non-deleted) records only.
    """
    stmt = select(BranchOperatingHours).where(
        BranchOperatingHours.branch_id == branch_id,
        BranchOperatingHours.deleted_at.is_(None)
    ).order_by(BranchOperatingHours.day_of_week, BranchOperatingHours.slot_index)
    
    hours = (await db.scalars(stmt)).all()
    return hours


@router.put("/branches/{branch_id}/hours")
async def update_branch_operating_hours(
    branch_id: uuid.UUID,
    payload: BulkOperatingHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_branch_staff_role(["manager"]))
):
    """
    Bulk replaces the standard weekly operating hours for a specific branch.
    Requires Branch Manager or Org Admin access.
    """
    # Soft delete existing active schedules
    stmt_del = update(BranchOperatingHours).where(
        BranchOperatingHours.branch_id == branch_id,
        BranchOperatingHours.deleted_at.is_(None)
    ).values(deleted_at=datetime.now(timezone.utc), updated_by=staff.id)
    await db.execute(stmt_del)
    
    # Insert new ones
    if payload.schedules:
        db.add_all([
            BranchOperatingHours(
                branch_id=branch_id,
                day_of_week=sched.day_of_week,
                slot_index=sched.slot_index,
                valid_from=sched.valid_from,
                valid_until=sched.valid_until,
                open_time=sched.open_time,
                close_time=sched.close_time,
                is_closed=sched.is_closed,
                is_24_hours=sched.is_24_hours,
                created_by=staff.id,
                updated_by=staff.id
            ) for sched in payload.schedules
        ])
        
    await db.commit()
    return {"status": "success", "message": "Branch standard hours updated"}


@router.get("/branches/{branch_id}/special-hours", response_model=List[BranchSpecialHoursResponse])
async def get_branch_special_hours(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff)
):
    """
    Returns the special exception hours for a specific branch.
    Includes active (non-deleted) records only.
    """
    stmt = select(BranchSpecialHours).where(
        BranchSpecialHours.branch_id == branch_id,
        BranchSpecialHours.deleted_at.is_(None)
    ).order_by(BranchSpecialHours.special_date, BranchSpecialHours.open_time)
    
    hours = (await db.scalars(stmt)).all()
    return hours


@router.put("/branches/{branch_id}/special-hours")
async def update_branch_special_hours(
    branch_id: uuid.UUID,
    payload: BulkSpecialHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_branch_staff_role(["manager"]))
):
    """
    Sets special holiday/exception hours.
    To allow for clean overlap handling, this bulk replaces ALL existing active special hours for the branch.
    """
    # Soft delete all existing active special hours for this branch
    stmt_del = update(BranchSpecialHours).where(
        BranchSpecialHours.branch_id == branch_id,
        BranchSpecialHours.deleted_at.is_(None)
    ).values(deleted_at=datetime.now(timezone.utc), updated_by=staff.id)
    await db.execute(stmt_del)
    
    # Insert new
    if payload.schedules:
        db.add_all([
            BranchSpecialHours(
                branch_id=branch_id,
                special_date=sched.special_date,
                open_time=sched.open_time,
                close_time=sched.close_time,
                is_closed=sched.is_closed,
                is_24_hours=sched.is_24_hours,
                reason=sched.reason,
                created_by=staff.id,
                updated_by=staff.id
            ) for sched in payload.schedules
        ])
    
    await db.commit()
    return {"status": "success", "message": "Branch special hours updated"}


@router.get("/organizations/hours", response_model=List[OrganizationOperatingHoursResponse])
async def get_organization_operating_hours(
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff)
):
    """
    Returns the organization-wide default operating hours.
    Includes active (non-deleted) records only.
    """
    org_id = staff.org_id
    stmt = select(OrganizationOperatingHours).where(
        OrganizationOperatingHours.org_id == org_id,
        OrganizationOperatingHours.deleted_at.is_(None)
    ).order_by(OrganizationOperatingHours.day_of_week, OrganizationOperatingHours.slot_index)
    
    hours = (await db.scalars(stmt)).all()
    return hours


@router.put("/organizations/hours")
async def update_organization_operating_hours(
    payload: BulkOperatingHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    """
    Bulk replaces the organization-wide default operating hours.
    Requires Org Admin access.
    """
    org_id = staff.org_id
    
    # Soft delete existing active schedules
    stmt_del = update(OrganizationOperatingHours).where(
        OrganizationOperatingHours.org_id == org_id,
        OrganizationOperatingHours.deleted_at.is_(None)
    ).values(deleted_at=datetime.now(timezone.utc), updated_by=staff.id)
    await db.execute(stmt_del)
    
    # Insert new
    if payload.schedules:
        db.add_all([
            OrganizationOperatingHours(
                org_id=org_id,
                day_of_week=sched.day_of_week,
                slot_index=sched.slot_index,
                valid_from=sched.valid_from,
                valid_until=sched.valid_until,
                open_time=sched.open_time,
                close_time=sched.close_time,
                is_closed=sched.is_closed,
                is_24_hours=sched.is_24_hours,
                created_by=staff.id,
                updated_by=staff.id
            ) for sched in payload.schedules
        ])
        
    await db.commit()
    return {"status": "success", "message": "Organization default hours updated"}
