from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..schemas.plan import PlanCreate, PlanUpdate, PlanRead
from ..models.models import SubscriptionPlan, StaffRole

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get('/', response_model=list[PlanRead])
async def list_plans(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    """List all active subscription plans for the current org."""
    stmt = (
        select(SubscriptionPlan)
        .where(
            SubscriptionPlan.org_id == context.org_id,
            SubscriptionPlan.deleted_at.is_(None),
        )
        .order_by(SubscriptionPlan.price.asc())
    )
    q = await db.execute(stmt)
    return q.scalars().all()


@router.post('/', response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    """Create a new subscription plan for the current org."""
    plan = SubscriptionPlan(
        org_id=context.org_id,
        name=payload.name.strip(),
        duration_days=payload.duration_days,
        price=payload.price,
        grace_period_days=payload.grace_period_days,
        is_active=True,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put('/{plan_id}', response_model=PlanRead)
async def update_plan(
    plan_id: str,
    payload: PlanUpdate,
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing subscription plan.
    
    Production note: Changing price or duration does NOT affect existing
    subscriptions — only new ones created after the update.
    """
    stmt = select(SubscriptionPlan).where(
        SubscriptionPlan.id == plan_id,
        SubscriptionPlan.org_id == context.org_id,
        SubscriptionPlan.deleted_at.is_(None),
    )
    q = await db.execute(stmt)
    plan = q.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete('/{plan_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(
    plan_id: str,
    context: TenantContext = Depends(RequireRole([StaffRole.owner])),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a subscription plan.
    
    Only owners can delete plans. Existing subscriptions linked to this
    plan are NOT affected (RESTRICT FK prevents orphaning).
    """
    stmt = select(SubscriptionPlan).where(
        SubscriptionPlan.id == plan_id,
        SubscriptionPlan.org_id == context.org_id,
        SubscriptionPlan.deleted_at.is_(None),
    )
    q = await db.execute(stmt)
    plan = q.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    plan.deleted_at = datetime.now(timezone.utc)
    plan.is_active = False
    await db.commit()
    return None
