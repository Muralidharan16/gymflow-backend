from app.tasks.celery_app import app
from app.core.database import AsyncSessionLocal
from app.models.subscription import MemberSubscription
from app.models.member import Member
from app.models.enums import SubscriptionStatus, MemberStatus
from app.core.redis import redis_client
from sqlalchemy import select, update
from datetime import date
import asyncio

async def expire_subscriptions_task():
    async with AsyncSessionLocal() as session:
        today = date.today()
        
        # 1. Get expiring subs
        q = select(MemberSubscription).where(
            MemberSubscription.end_date < today,
            MemberSubscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.frozen])
        )
        result = await session.execute(q)
        subs = result.scalars().all()
        
        if not subs:
            return 0
            
        sub_ids = [s.id for s in subs]
        member_ids = [s.member_id for s in subs]
        
        # 2. Bulk Update Subs
        await session.execute(
            update(MemberSubscription)
            .where(MemberSubscription.id.in_(sub_ids))
            .values(status=SubscriptionStatus.expired)
        )
        
        # 3. Bulk Update Members
        await session.execute(
            update(Member)
            .where(Member.id.in_(member_ids))
            .values(status=MemberStatus.expired)
        )
        
        await session.commit()
        
        # 4. Redis Invalidation
        q_members = select(Member).where(Member.id.in_(member_ids))
        res_members = await session.execute(q_members)
        members = res_members.scalars().all()
        
        for m in members:
            await redis_client.delete(f"{m.qr_token}:access")
            await redis_client.delete(f"{m.member_uid}:access")
            
        return len(sub_ids)

@app.task(name="app.tasks.expire_subs.run")
def run():
    loop = asyncio.get_event_loop()
    count = loop.run_until_complete(expire_subscriptions_task())
    return f"Expired {count} subscriptions"
