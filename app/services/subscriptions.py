import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..models.models import (
    Member, MemberSubscription, Payment, SubscriptionPlan,
    PaymentStatus, PaymentSource, PaymentMethod, SubscriptionStatus, RenewalType,
    MemberStatus,
)
from ..redis_client import redis_client

logger = logging.getLogger(__name__)


async def process_idempotent_payment(
    db: AsyncSession,
    idempotency_key: str,
    org_id: str,
    branch_id: str,
    member_id: str,
    plan_id: str,
    amount_paid: float,
    payment_method: str,
    payment_source: str,
    renewal_type: str,
    staff_id: str,
) -> dict:
    """
    Atomically creates a Subscription + Payment and updates the member's
    current_subscription_id pointer.

    Design decisions:
    - Idempotency via Redis key (24h TTL) prevents double-charges on slow networks.
    - Uses begin_nested() for transactional atomicity.
    - Validates plan and member belong to the same org (tenant isolation).
    - Returns structured dict for the router to serialize.
    """
    # --- Idempotency check ---
    idemp_redis_key = f"payment:idempotency:{org_id}:{idempotency_key}"
    if redis_client:
        try:
            cached = await redis_client.get(idemp_redis_key)
            if cached:
                logger.info(f"Idempotent hit for key={idempotency_key}")
                return {"status": "already_processed", "payment_id": cached}
        except Exception as e:
            logger.error(f"Redis idempotency check failed: {e}")

    # --- Tenant-isolated validation ---
    plan = await _get_plan(db, plan_id, org_id)
    if not plan:
        raise ValueError("Invalid or inactive subscription plan")

    member = await _get_member(db, member_id, org_id)
    if not member:
        raise ValueError("Member not found in this organization")

    now = datetime.now(timezone.utc)
    start_date = now.date()
    end_date = start_date + timedelta(days=plan.duration_days)
    grace_until = end_date + timedelta(days=plan.grace_period_days)

    try:
        async with db.begin_nested() if db.in_transaction() else db.begin():
            # 1. Create Subscription
            new_sub = MemberSubscription(
                org_id=org_id,
                branch_id=branch_id,
                member_id=member_id,
                plan_id=plan_id,
                status=SubscriptionStatus.active,
                renewal_type=RenewalType(renewal_type),
                start_date=start_date,
                end_date=end_date,
                grace_until=grace_until,
                created_by=staff_id,
            )
            db.add(new_sub)
            await db.flush()

            # 2. Create Payment linked to subscription
            new_payment = Payment(
                org_id=org_id,
                branch_id=branch_id,
                member_id=member_id,
                subscription_id=new_sub.id,
                amount=amount_paid,
                payment_method=PaymentMethod(payment_method),
                payment_source=PaymentSource(payment_source),
                status=PaymentStatus.success,
                payment_date=now,
                idempotency_key=idempotency_key,
                created_by=staff_id,
            )
            db.add(new_payment)

            # 3. Update member's current subscription pointer
            member.current_subscription_id = new_sub.id
            member.is_active = True
            member.status = MemberStatus.active

        await db.commit()
    except IntegrityError:
        # DB-level idempotency catch: unique index on (org_id, idempotency_key)
        await db.rollback()
        logger.info(f"DB-level idempotent hit for key={idempotency_key}")
        return {"status": "already_processed", "payment_id": None}

    # --- Cache idempotency key for 24 hours ---
    if redis_client:
        try:
            await redis_client.setex(idemp_redis_key, 86400, str(new_payment.id))
        except Exception as e:
            logger.error(f"Redis idempotency set failed: {e}")

    return {
        "status": "success",
        "payment_id": str(new_payment.id),
        "subscription_id": str(new_sub.id),
        "start_date": str(start_date),
        "end_date": str(end_date),
    }


async def _get_plan(db: AsyncSession, plan_id: str, org_id: str):
    stmt = select(SubscriptionPlan).where(
        SubscriptionPlan.id == plan_id,
        SubscriptionPlan.org_id == org_id,
        SubscriptionPlan.is_active.is_(True),
        SubscriptionPlan.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_member(db: AsyncSession, member_id: str, org_id: str):
    stmt = select(Member).where(
        Member.id == member_id,
        Member.org_id == org_id,
        Member.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
