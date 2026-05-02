from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, timedelta
from ..database import get_db
from ..middleware.auth_middleware import get_current_owner
from ..schemas.subscription import SubscriptionCreate, SubscriptionRenew, ExpiringMember
from ..models.models import Member, SubscriptionPlan, MemberSubscription, SubscriptionStatus

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get('/expiring', response_model=list[ExpiringMember])
async def expiring(gym_owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    gym_id = gym_owner.gym_id
    today = date.today()
    window_end = today + timedelta(days=3)
    q = await db.execute(
        """
        SELECT ms.id, ms.member_id, m.name, m.phone, m.email, ms.end_date
        FROM member_subscriptions ms
        JOIN members m ON m.id = ms.member_id
        WHERE m.gym_id = :gym_id AND ms.status = 'active' AND ms.end_date BETWEEN :today AND :window_end
        ORDER BY ms.end_date ASC
        """,
        {"gym_id": str(gym_id), "today": today, "window_end": window_end},
    )
    rows = q.fetchall()
    results = []
    for r in rows:
        results.append(ExpiringMember(member_id=str(r[1]), member_name=r[2], phone=r[3], email=r[4], end_date=r[5]))
    return results


@router.post('/', status_code=status.HTTP_201_CREATED)
async def create_subscription(payload: SubscriptionCreate, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    # validate member
    q = await db.execute(select(Member).where(Member.id == payload.member_id, Member.gym_id == owner.gym_id))
    member = q.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')

    q = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == payload.plan_id, SubscriptionPlan.gym_id == owner.gym_id))
    plan = q.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Plan not found')

    start_dt = payload.start_date or date.today()
    end_dt = start_dt + timedelta(days=plan.duration_days - 1)

    sub = MemberSubscription(member_id=member.id, plan_id=plan.id, start_date=start_dt, end_date=end_dt, status=SubscriptionStatus.active)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"id": str(sub.id), "member_id": str(member.id), "start_date": str(sub.start_date), "end_date": str(sub.end_date)}


@router.put('/{subscription_id}/renew')
async def renew_subscription(subscription_id: str, payload: SubscriptionRenew, db: AsyncSession = Depends(get_db), owner = Depends(get_current_owner)):
    q = await db.execute(select(MemberSubscription).where(MemberSubscription.id == subscription_id))
    sub = q.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')

    # ensure member belongs to owner's gym
    q = await db.execute(select(Member).where(Member.id == sub.member_id, Member.gym_id == owner.gym_id))
    member = q.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Permission denied')

    # determine plan duration
    plan_id = payload.renewal_plan_id or str(sub.plan_id)
    q = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id, SubscriptionPlan.gym_id == owner.gym_id))
    plan = q.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Plan not found')

    # extend end_date
    sub.end_date = sub.end_date + timedelta(days=plan.duration_days)
    sub.status = SubscriptionStatus.active
    if payload.payment_id:
        sub.payment_id = payload.payment_id
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"id": str(sub.id), "new_end_date": str(sub.end_date)}
