from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, func, and_, extract
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff
from app.models.staff import Staff
from app.models.gym import Gym
from app.models.member import Member
from app.models.member_subscription import MemberSubscription, SubscriptionStatus
from app.models.payment import Payment, PaymentStatus, PaymentMethod
from app.models.attendance import AttendanceLog
from app.schemas.common import Response
from app.schemas.reports import (
    DashboardResponse, ExpiringMemberResponse, CollectionSummaryResponse,
    AttendanceHeatmapResponse, HourlyCount
)

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("/dashboard", response_model=Response[DashboardResponse])
async def get_dashboard(
    gym_id: Optional[UUID] = Query(None, description="Optional gym ID; if not provided, aggregates over entire org"),
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get dashboard metrics: total revenue month, active members, new members month,
    expired month, churn rate.
    Scoped to organization (all gyms) or specific gym.
    """
    org_id = current_staff.org_id
    today = date.today()
    first_of_month = date(today.year, today.month, 1)
    
    # Base gyms filter
    gyms_query = select(Gym.id).where(Gym.org_id == org_id, Gym.is_active == True)
    if gym_id:
        gyms_query = gyms_query.where(Gym.id == gym_id)
    gyms_result = await db.execute(gyms_query)
    gym_ids = [row[0] for row in gyms_result.all()]
    
    if not gym_ids:
        return Response(data=DashboardResponse(
            total_revenue_month=Decimal('0'),
            active_members=0,
            new_members_month=0,
            expired_month=0,
            churn_rate=0.0
        ))
    
    # 1. Total revenue this month (completed payments)
    revenue_query = select(func.sum(Payment.amount)).where(
        Payment.gym_id.in_(gym_ids),
        Payment.status == PaymentStatus.COMPLETED,
        Payment.payment_date >= first_of_month
    )
    revenue_result = await db.execute(revenue_query)
    total_revenue = revenue_result.scalar() or Decimal('0')
    
    # 2. Active members (active subscriptions with end_date >= today)
    active_query = select(func.count(func.distinct(MemberSubscription.member_id))).where(
        MemberSubscription.gym_id.in_(gym_ids),
        MemberSubscription.status == SubscriptionStatus.ACTIVE,
        MemberSubscription.end_date >= today
    )
    active_result = await db.execute(active_query)
    active_members = active_result.scalar() or 0
    
    # 3. New members this month (created_at >= first_of_month)
    new_query = select(func.count(Member.id)).where(
        Member.gym_id.in_(gym_ids),
        Member.is_active == True,
        Member.created_at >= first_of_month
    )
    new_result = await db.execute(new_query)
    new_members = new_result.scalar() or 0
    
    # 4. Expired subscriptions this month (status=expired AND end_date this month)
    # Note: We count subscriptions, not members, as per spec
    expired_query = select(func.count(MemberSubscription.id)).where(
        MemberSubscription.gym_id.in_(gym_ids),
        MemberSubscription.status == SubscriptionStatus.EXPIRED,
        MemberSubscription.end_date >= first_of_month,
        MemberSubscription.end_date <= today
    )
    expired_result = await db.execute(expired_query)
    expired_count = expired_result.scalar() or 0
    
    # 5. Churn rate: (expired_month / (active_members + expired_month)) * 100
    denominator = active_members + expired_count
    churn_rate = round((expired_count / denominator) * 100, 2) if denominator > 0 else 0.0
    
    return Response(data=DashboardResponse(
        total_revenue_month=total_revenue,
        active_members=active_members,
        new_members_month=new_members,
        expired_month=expired_count,
        churn_rate=churn_rate
    ))


@router.get("/expiring", response_model=Response[List[ExpiringMemberResponse]])
async def get_expiring_subscriptions(
    days: int = Query(7, ge=1, le=30, description="Number of days to look ahead"),
    gym_id: Optional[UUID] = Query(None, description="Optional gym ID"),
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get list of members whose active subscriptions expire within the next N days.
    """
    org_id = current_staff.org_id
    today = date.today()
    future_date = today + timedelta(days=days)
    
    # Gym filter
    gyms_query = select(Gym.id).where(Gym.org_id == org_id, Gym.is_active == True)
    if gym_id:
        gyms_query = gyms_query.where(Gym.id == gym_id)
    gyms_result = await db.execute(gyms_query)
    gym_ids = [row[0] for row in gyms_result.all()]
    
    if not gym_ids:
        return Response(data=[])
    
    # Query subscriptions with status ACTIVE and end_date between today and future_date
    query = (
        select(MemberSubscription, Member)
        .join(Member, MemberSubscription.member_id == Member.id)
        .where(
            MemberSubscription.gym_id.in_(gym_ids),
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date >= today,
            MemberSubscription.end_date <= future_date
        )
        .order_by(MemberSubscription.end_date)
    )
    result = await db.execute(query)
    rows = result.all()
    
    expiring = []
    for sub, member in rows:
        expiring.append(ExpiringMemberResponse(
            member_id=member.id,
            member_name=member.name,
            phone=member.phone,
            subscription_id=sub.id,
            plan_name=sub.plan.name if sub.plan else "Unknown",
            end_date=sub.end_date,
            days_remaining=(sub.end_date - today).days
        ))
    
    return Response(data=expiring)


@router.get("/collections", response_model=Response[List[CollectionSummaryResponse]])
async def get_collections_summary(
    date_from: date = Query(..., description="Start date (YYYY-MM-DD)"),
    date_to: date = Query(..., description="End date (YYYY-MM-DD)"),
    gym_id: Optional[UUID] = Query(None, description="Optional gym ID"),
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get payment collections grouped by payment method within date range.
    """
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from must be <= date_to")
    
    org_id = current_staff.org_id
    
    # Gym filter
    gyms_query = select(Gym.id).where(Gym.org_id == org_id, Gym.is_active == True)
    if gym_id:
        gyms_query = gyms_query.where(Gym.id == gym_id)
    gyms_result = await db.execute(gyms_query)
    gym_ids = [row[0] for row in gyms_result.all()]
    
    if not gym_ids:
        return Response(data=[])
    
    # Query sum and count grouped by payment method
    from_dt = datetime.combine(date_from, datetime.min.time())
    to_dt = datetime.combine(date_to, datetime.max.time())
    
    query = (
        select(
            Payment.method,
            func.sum(Payment.amount).label("total"),
            func.count().label("count")
        )
        .where(
            Payment.gym_id.in_(gym_ids),
            Payment.status == PaymentStatus.COMPLETED,
            Payment.payment_date >= from_dt,
            Payment.payment_date <= to_dt
        )
        .group_by(Payment.method)
    )
    result = await db.execute(query)
    rows = result.all()
    
    collections = []
    for row in rows:
        method = row.method
        collections.append(CollectionSummaryResponse(
            payment_method=method.value if method else "UNKNOWN",
            total_amount=row.total or Decimal('0'),
            count=row.count or 0
        ))
    
    return Response(data=collections)


@router.get("/attendance", response_model=Response[AttendanceHeatmapResponse])
async def get_attendance_heatmap(
    days: int = Query(30, ge=1, le=90, description="Number of days to look back (default 30)"),
    gym_id: Optional[UUID] = Query(None, description="Optional gym ID"),
    current_staff: Staff = Depends(get_current_active_staff),
    db: AsyncSession = Depends(get_db)
):
    """
    Get attendance heatmap: count of check-ins per hour for the last N days.
    Returns list of {hour, count} for hours 0-23.
    """
    org_id = current_staff.org_id
    cutoff = datetime.now() - timedelta(days=days)
    
    # Gym filter
    gyms_query = select(Gym.id).where(Gym.org_id == org_id, Gym.is_active == True)
    if gym_id:
        gyms_query = gyms_query.where(Gym.id == gym_id)
    gyms_result = await db.execute(gyms_query)
    gym_ids = [row[0] for row in gyms_result.all()]
    
    if not gym_ids:
        # Return zeros for all hours
        return Response(data=AttendanceHeatmapResponse(
            hours=[HourlyCount(hour=h, count=0) for h in range(24)],
            days_analyzed=days
        ))
    
    # PostgreSQL: extract hour from check_in_time where granted = True
    query = (
        select(
            extract('hour', AttendanceLog.check_in_time).label('hour'),
            func.count().label('count')
        )
        .where(
            AttendanceLog.gym_id.in_(gym_ids),
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= cutoff
        )
        .group_by('hour')
        .order_by('hour')
    )
    result = await db.execute(query)
    rows = result.all()
    
    # Build hour->count map
    count_by_hour = {int(row.hour): row.count for row in rows}
    
    # Fill all hours 0-23
    hourly_counts = []
    for h in range(24):
        hourly_counts.append(HourlyCount(hour=h, count=count_by_hour.get(h, 0)))
    
    return Response(data=AttendanceHeatmapResponse(
        hours=hourly_counts,
        days_analyzed=days
    ))