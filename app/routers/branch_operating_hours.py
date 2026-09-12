from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, text
import uuid
from typing import List
from datetime import datetime, timezone

from app.core.database import get_db
from app.core.deps import (
    get_current_active_staff,
    require_org_admin,
    require_branch_staff_role,
    Staff,
)
from app.schemas.branch_operating_hours import (
    BranchHoursProjectionResponse,
    BulkOperatingHoursRequest,
    BulkSpecialHoursRequest,
    BranchOperatingHoursResponse,
    BranchSpecialHoursResponse,
    OrganizationOperatingHoursResponse,
)
from app.models.branch_operating_hours import (
    BranchOperatingHours,
    OrganizationOperatingHours,
    BranchSpecialHours,
    BranchHoursProjection,
)

router = APIRouter(tags=["operating-hours"])

_BRANCH_HOURS_LOCK_SEED = 72810431


async def _acquire_hours_replacement_lock(db: AsyncSession, scope: str) -> None:
    """Acquire a transaction-scoped, non-blocking mutex for bulk replacement.

    A bulk PUT is a logical replace operation, not independent row updates.  The
    lock prevents two concurrent replacements for the same branch/org from
    interleaving into a blended schedule.  Non-blocking acquisition preserves
    the bounded API latency contract; callers receive a retryable 409 instead
    of sitting on a database lock until the statement timeout expires.
    """

    acquired = await db.scalar(
        text(
            """
            SELECT pg_catalog.pg_try_advisory_xact_lock(
                pg_catalog.hashtextextended(:scope, :seed)
            )
            """
        ),
        {"scope": scope, "seed": _BRANCH_HOURS_LOCK_SEED},
    )
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operating hours are being updated concurrently. Retry the request.",
        )


async def _enqueue_branch_projection(
    db: AsyncSession,
    *,
    branch_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> None:
    await db.execute(
        text(
            """
            SELECT public.enqueue_branch_hours_rebuild(
                :branch_id,
                :correlation_id
            )
            """
        ),
        {"branch_id": branch_id, "correlation_id": correlation_id},
    )


async def _enqueue_org_projection_fanout(
    db: AsyncSession,
    *,
    correlation_id: uuid.UUID,
) -> None:
    await db.execute(
        text(
            """
            SELECT public.enqueue_organization_hours_rebuild(:correlation_id)
            """
        ),
        {"correlation_id": correlation_id},
    )


@router.get(
    "/branches/{branch_id}/hours/projection",
    response_model=BranchHoursProjectionResponse,
)
async def get_branch_hours_projection(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the cached branch-hours projection visible to this tenant."""

    stmt = select(BranchHoursProjection).where(
        BranchHoursProjection.branch_id == branch_id
    )
    projection = await db.scalar(stmt)

    if not projection:
        raise HTTPException(
            status_code=404,
            detail="Projection not found for this branch. Has not been configured yet.",
        )

    return projection


@router.get(
    "/branches/{branch_id}/hours",
    response_model=List[BranchOperatingHoursResponse],
)
async def get_branch_operating_hours(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff),
):
    """Return active standard weekly hours for a branch."""

    stmt = (
        select(BranchOperatingHours)
        .where(
            BranchOperatingHours.branch_id == branch_id,
            BranchOperatingHours.deleted_at.is_(None),
        )
        .order_by(
            BranchOperatingHours.day_of_week,
            BranchOperatingHours.slot_index,
        )
    )
    return (await db.scalars(stmt)).all()


@router.put("/branches/{branch_id}/hours")
async def update_branch_operating_hours(
    branch_id: uuid.UUID,
    payload: BulkOperatingHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_branch_staff_role(["manager"])),
):
    """Atomically replace a branch's standard hours and enqueue one rebuild."""

    await _acquire_hours_replacement_lock(
        db,
        f"branch-hours:branch:{branch_id}",
    )
    correlation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    await db.execute(
        update(BranchOperatingHours)
        .where(
            BranchOperatingHours.branch_id == branch_id,
            BranchOperatingHours.deleted_at.is_(None),
        )
        .values(deleted_at=now, updated_by=staff.id)
    )

    if payload.schedules:
        db.add_all(
            [
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
                    updated_by=staff.id,
                )
                for sched in payload.schedules
            ]
        )

    # Flush schedule rows/audit effects before recording durable work.  Any
    # constraint/RLS failure aborts the transaction and therefore the enqueue.
    await db.flush()
    await _enqueue_branch_projection(
        db,
        branch_id=branch_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    return {
        "status": "success",
        "message": "Branch standard hours updated",
        "correlation_id": str(correlation_id),
    }


@router.get(
    "/branches/{branch_id}/special-hours",
    response_model=List[BranchSpecialHoursResponse],
)
async def get_branch_special_hours(
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff),
):
    """Return active special/exception hours for a branch."""

    stmt = (
        select(BranchSpecialHours)
        .where(
            BranchSpecialHours.branch_id == branch_id,
            BranchSpecialHours.deleted_at.is_(None),
        )
        .order_by(
            BranchSpecialHours.special_date,
            BranchSpecialHours.open_time,
        )
    )
    return (await db.scalars(stmt)).all()


@router.put("/branches/{branch_id}/special-hours")
async def update_branch_special_hours(
    branch_id: uuid.UUID,
    payload: BulkSpecialHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_branch_staff_role(["manager"])),
):
    """Atomically replace branch special hours and enqueue one rebuild."""

    await _acquire_hours_replacement_lock(
        db,
        f"branch-hours:branch:{branch_id}",
    )
    correlation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    await db.execute(
        update(BranchSpecialHours)
        .where(
            BranchSpecialHours.branch_id == branch_id,
            BranchSpecialHours.deleted_at.is_(None),
        )
        .values(deleted_at=now, updated_by=staff.id)
    )

    if payload.schedules:
        db.add_all(
            [
                BranchSpecialHours(
                    branch_id=branch_id,
                    special_date=sched.special_date,
                    open_time=sched.open_time,
                    close_time=sched.close_time,
                    is_closed=sched.is_closed,
                    is_24_hours=sched.is_24_hours,
                    reason=sched.reason,
                    created_by=staff.id,
                    updated_by=staff.id,
                )
                for sched in payload.schedules
            ]
        )

    await db.flush()
    await _enqueue_branch_projection(
        db,
        branch_id=branch_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    return {
        "status": "success",
        "message": "Branch special hours updated",
        "correlation_id": str(correlation_id),
    }


@router.get(
    "/organizations/hours",
    response_model=List[OrganizationOperatingHoursResponse],
)
async def get_organization_operating_hours(
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(get_current_active_staff),
):
    """Return active organization-wide default operating hours."""

    stmt = (
        select(OrganizationOperatingHours)
        .where(
            OrganizationOperatingHours.org_id == staff.org_id,
            OrganizationOperatingHours.deleted_at.is_(None),
        )
        .order_by(
            OrganizationOperatingHours.day_of_week,
            OrganizationOperatingHours.slot_index,
        )
    )
    return (await db.scalars(stmt)).all()


@router.put("/organizations/hours")
async def update_organization_operating_hours(
    payload: BulkOperatingHoursRequest,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    """Atomically replace org defaults and enqueue one durable fan-out event."""

    org_id = staff.org_id
    await _acquire_hours_replacement_lock(
        db,
        f"branch-hours:org:{org_id}",
    )
    correlation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    await db.execute(
        update(OrganizationOperatingHours)
        .where(
            OrganizationOperatingHours.org_id == org_id,
            OrganizationOperatingHours.deleted_at.is_(None),
        )
        .values(deleted_at=now, updated_by=staff.id)
    )

    if payload.schedules:
        db.add_all(
            [
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
                    updated_by=staff.id,
                )
                for sched in payload.schedules
            ]
        )

    await db.flush()
    await _enqueue_org_projection_fanout(
        db,
        correlation_id=correlation_id,
    )
    await db.commit()
    return {
        "status": "success",
        "message": "Organization default hours updated",
        "correlation_id": str(correlation_id),
    }
