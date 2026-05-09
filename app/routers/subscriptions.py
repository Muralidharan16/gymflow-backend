from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, timedelta
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..schemas.subscription import ExpiringMember
from ..models.models import Member, MemberSubscription, SubscriptionStatus, StaffRole

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get('/expiring', response_model=list[ExpiringMember])
async def expiring(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    """List members whose subscriptions expire within the next 3 days."""
    today = date.today()
    window_end = today + timedelta(days=3)

    stmt = (
        select(
            Member.id,
            Member.name,
            Member.member_uid,
            Member.phone,
            MemberSubscription.end_date,
        )
        .join(MemberSubscription, MemberSubscription.member_id == Member.id)
        .where(
            Member.org_id == context.org_id,
            Member.deleted_at.is_(None),
            MemberSubscription.status == SubscriptionStatus.active,
            MemberSubscription.end_date.between(today, window_end),
        )
        .order_by(MemberSubscription.end_date.asc())
    )

    q = await db.execute(stmt)
    rows = q.fetchall()

    return [
        ExpiringMember(
            member_id=str(r[0]),
            member_name=r[1],
            member_uid=r[2],
            phone=r[3],
            end_date=r[4],
        )
        for r in rows
    ]
