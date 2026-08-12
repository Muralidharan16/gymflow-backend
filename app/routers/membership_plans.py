import uuid
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_org_admin, Staff
from app.models.membership_plan import MembershipPlan, PlanStatus
from app.models.org_branch import OrgBranch
from app.schemas.membership_plan import (
    MembershipPlanCreate,
    MembershipPlanUpdate,
    MembershipPlanResponse
)

router = APIRouter(prefix="/membership-plans", tags=["Membership Plans"])

def clean_slug(slug: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9]', '', slug)
    return cleaned[:6].upper()

async def get_next_sequence_for_org(db: AsyncSession, org_id: uuid.UUID) -> int:
    query = text("""
        INSERT INTO organization_counters (id, org_id, counter_key, current_value)
        VALUES (:id, :org_id, 'membership_plan', 1)
        ON CONFLICT (org_id, counter_key)
        DO UPDATE SET current_value = organization_counters.current_value + 1
        RETURNING current_value;
    """)
    result = await db.execute(query, {"id": uuid.uuid4(), "org_id": org_id})
    return result.scalar()

@router.post("", response_model=MembershipPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_membership_plan(
    data: MembershipPlanCreate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    # branch_id validation
    if data.branch_id:
        branch_query = select(OrgBranch).where(
            OrgBranch.id == data.branch_id, OrgBranch.org_id == staff.org_id
        )
        branch_result = await db.execute(branch_query)
        if not branch_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Branch not found or does not belong to organization")

    # Tenant metadata is deliberately exposed through narrow SECURITY DEFINER
    # capabilities bound to the transaction-local app.current_org_id. Ordinary
    # API runtime must never need SELECT on the organizations base table.
    org_metadata = (
        await db.execute(
            select(
                func.public.current_organization_slug().label("slug"),
                func.public.current_organization_default_currency_code().label(
                    "default_currency_code"
                ),
            )
        )
    ).one()
    org_slug = org_metadata.slug
    org_currency = org_metadata.default_currency_code

    # default_currency_code is NOT NULL for a persisted organization. A NULL
    # capability result therefore means the trusted tenant context did not map
    # to an organization; fail closed rather than falling back to table access.
    if org_currency is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    prefix = clean_slug(org_slug) if org_slug else "PLAN"
    if not prefix:
        prefix = "PLAN"

    seq = await get_next_sequence_for_org(db, staff.org_id)
    plan_code = f"{prefix}-{seq:03d}"

    plan = MembershipPlan(
        org_id=staff.org_id,
        branch_id=data.branch_id,
        plan_code=plan_code,
        name=data.name,
        description=data.description,
        price=data.price,
        currency=org_currency,
        duration_value=data.duration_value,
        duration_unit=data.duration_unit,
        max_members=data.max_members,
        valid_from=data.valid_from,
        valid_until=data.valid_until,
        status=PlanStatus.active,
        created_by=staff.id
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan

@router.get("", response_model=List[MembershipPlanResponse])
async def list_membership_plans(
    plan_status: Optional[PlanStatus] = None,
    branch_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(MembershipPlan.org_id == staff.org_id)

    if plan_status:
        query = query.where(MembershipPlan.status == plan_status)
    if branch_id:
        query = query.where(MembershipPlan.branch_id == branch_id)

    query = query.order_by(MembershipPlan.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())

@router.get("/{plan_id}", response_model=MembershipPlanResponse)
async def get_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id, MembershipPlan.org_id == staff.org_id
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan

@router.patch("/{plan_id}", response_model=MembershipPlanResponse)
async def update_membership_plan(
    plan_id: uuid.UUID,
    data: MembershipPlanUpdate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id, MembershipPlan.org_id == staff.org_id
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan.status == PlanStatus.archived:
        # only allow updates if specifically allowed, but generally not normal fields
        raise HTTPException(status_code=400, detail="Cannot update archived plans")

    if data.valid_until and data.valid_from:
        if data.valid_until <= data.valid_from:
            raise HTTPException(status_code=400, detail="valid_until must be after valid_from")
    elif data.valid_until and plan.valid_from:
        # Pydantic strips timezone sometimes, let's just do naive comparison if we have to,
        # or SQLAlchemy handles it. Let's assume standard behavior.
        if data.valid_until <= plan.valid_from:
            raise HTTPException(status_code=400, detail="valid_until must be after valid_from")
    elif data.valid_from and plan.valid_until:
        if plan.valid_until <= data.valid_from:
            raise HTTPException(status_code=400, detail="valid_until must be after valid_from")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan

@router.post("/{plan_id}/archive", response_model=MembershipPlanResponse)
async def archive_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id, MembershipPlan.org_id == staff.org_id
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan.status = PlanStatus.archived
    plan.archived_at = func.now()
    await db.commit()
    await db.refresh(plan)
    return plan

@router.post("/{plan_id}/activate", response_model=MembershipPlanResponse)
async def activate_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id, MembershipPlan.org_id == staff.org_id
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan.status == PlanStatus.archived:
        raise HTTPException(status_code=400, detail="Cannot reactivate archived plan")

    plan.status = PlanStatus.active
    await db.commit()
    await db.refresh(plan)
    return plan

@router.post("/{plan_id}/deactivate", response_model=MembershipPlanResponse)
async def deactivate_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin)
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id, MembershipPlan.org_id == staff.org_id
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan.status == PlanStatus.archived:
        raise HTTPException(status_code=400, detail="Plan is already archived")

    plan.status = PlanStatus.inactive
    await db.commit()
    await db.refresh(plan)
    return plan
