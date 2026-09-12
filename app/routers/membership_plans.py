import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import Staff, require_org_admin
from app.models.membership_plan import MembershipPlan, PlanStatus
from app.models.org_branch import OrgBranch
from app.schemas.membership_plan import (
    MembershipPlanCreate,
    MembershipPlanResponse,
    MembershipPlanUpdate,
)

router = APIRouter(prefix="/membership-plans", tags=["Membership Plans"])

_BRANCH_REFERENCE_CONSTRAINTS = frozenset(
    {
        "membership_plans_branch_id_fkey",
        "fk_membership_plans_branch_tenant",
    }
)


def clean_slug(slug: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", slug)
    return cleaned[:6].upper()


async def get_next_sequence_for_org(db: AsyncSession, org_id: uuid.UUID) -> int:
    # PostgreSQL serializes concurrent writers through the unique
    # (org_id, counter_key) row. Because this executes in the request's same
    # transaction as the plan INSERT, a failed plan write also rolls the
    # counter increment back.
    query = text(
        """
        INSERT INTO organization_counters (id, org_id, counter_key, current_value)
        VALUES (:id, :org_id, 'membership_plan', 1)
        ON CONFLICT (org_id, counter_key)
        DO UPDATE SET current_value = organization_counters.current_value + 1
        RETURNING current_value;
        """
    )
    result = await db.execute(
        query, {"id": uuid.uuid4(), "org_id": org_id}
    )
    return result.scalar_one()


def _validate_effective_validity_window(valid_from, valid_until) -> None:
    if (
        valid_from is not None
        and valid_until is not None
        and valid_until <= valid_from
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until must be after valid_from",
        )


def _postgres_constraint_name(exc: IntegrityError) -> str | None:
    """Extract a PostgreSQL constraint name through SQLAlchemy/asyncpg layers."""
    current: BaseException | None = exc.orig
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        constraint_name = getattr(current, "constraint_name", None)
        if constraint_name:
            return str(constraint_name)
        cause = getattr(current, "__cause__", None)
        current = cause if cause is not None else getattr(current, "__context__", None)
    return None


def _is_branch_reference_violation(exc: IntegrityError) -> bool:
    """Recognize only the two membership-plan branch FK violations."""
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
        exc.orig, "pgcode", None
    )
    if sqlstate != "23503":
        return False

    constraint_name = _postgres_constraint_name(exc)
    if constraint_name is not None:
        return constraint_name in _BRANCH_REFERENCE_CONSTRAINTS

    # SQLAlchemy's asyncpg adapter normally preserves the original asyncpg
    # exception as the cause. Keep a bounded fallback for adapter/version drift:
    # only SQLSTATE 23503 is eligible and only the two known constraint names
    # may be translated.
    message = str(exc.orig)
    return any(name in message for name in _BRANCH_REFERENCE_CONSTRAINTS)


@router.post(
    "",
    response_model=MembershipPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_membership_plan(
    data: MembershipPlanCreate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    # This SELECT gives a deterministic client error for an already-missing or
    # cross-tenant branch. It deliberately does not use SELECT ... FOR UPDATE /
    # KEY SHARE because that would require UPDATE privilege on org_branches and
    # violate the reduced app_runtime boundary. The composite database FK is
    # authoritative for a concurrent branch-delete race and acquires the
    # reference lock internally when the plan INSERT executes.
    if data.branch_id:
        branch_query = select(OrgBranch.id).where(
            OrgBranch.id == data.branch_id,
            OrgBranch.org_id == staff.org_id,
        )
        branch_result = await db.execute(branch_query)
        if branch_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Branch not found or does not belong to organization",
            )

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

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
        created_by=staff.id,
    )
    db.add(plan)
    try:
        await db.commit()
    except IntegrityError as exc:
        # A failed commit leaves the transaction unusable. Roll back before
        # translating the bounded branch-reference race; every other integrity
        # failure is re-raised so programming/data defects are never hidden.
        await db.rollback()
        if data.branch_id is not None and _is_branch_reference_violation(exc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Branch not found or does not belong to organization",
            ) from exc
        raise

    await db.refresh(plan)
    return plan


@router.get("", response_model=List[MembershipPlanResponse])
async def list_membership_plans(
    plan_status: Optional[PlanStatus] = None,
    branch_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.org_id == staff.org_id
    )

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
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id,
        MembershipPlan.org_id == staff.org_id,
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not found",
        )
    return plan


@router.patch("/{plan_id}", response_model=MembershipPlanResponse)
async def update_membership_plan(
    plan_id: uuid.UUID,
    data: MembershipPlanUpdate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id,
        MembershipPlan.org_id == staff.org_id,
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not found",
        )

    if plan.status == PlanStatus.archived:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update archived plans",
        )

    changes = data.model_dump(exclude_unset=True)
    effective_valid_from = changes.get("valid_from", plan.valid_from)
    effective_valid_until = changes.get("valid_until", plan.valid_until)
    _validate_effective_validity_window(
        effective_valid_from,
        effective_valid_until,
    )

    for field, value in changes.items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.post("/{plan_id}/archive", response_model=MembershipPlanResponse)
async def archive_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id,
        MembershipPlan.org_id == staff.org_id,
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not found",
        )

    plan.status = PlanStatus.archived
    plan.archived_at = func.now()
    await db.commit()
    await db.refresh(plan)
    return plan


@router.post("/{plan_id}/activate", response_model=MembershipPlanResponse)
async def activate_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id,
        MembershipPlan.org_id == staff.org_id,
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not found",
        )

    if plan.status == PlanStatus.archived:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reactivate archived plan",
        )

    plan.status = PlanStatus.active
    await db.commit()
    await db.refresh(plan)
    return plan


@router.post("/{plan_id}/deactivate", response_model=MembershipPlanResponse)
async def deactivate_membership_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    query = select(MembershipPlan).where(
        MembershipPlan.id == plan_id,
        MembershipPlan.org_id == staff.org_id,
    )
    result = await db.execute(query)
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan not found",
        )

    if plan.status == PlanStatus.archived:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plan is already archived",
        )

    plan.status = PlanStatus.inactive
    await db.commit()
    await db.refresh(plan)
    return plan
