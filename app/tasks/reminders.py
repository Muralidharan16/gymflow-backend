from app.tasks.celery_app import app
from app.core.database import AsyncSessionLocal
from app.models.subscription import MemberSubscription, SubscriptionPlan
from app.models.member import Member
from app.models.gym import Gym
from app.utils.whatsapp import send_whatsapp_template
from app.core.config import settings
from sqlalchemy import select, update
from datetime import date, timedelta
import asyncio

async def send_reminders_task():
    async with AsyncSessionLocal() as session:
        target_date = date.today() + timedelta(days=2)
        
        q = select(MemberSubscription, Member, SubscriptionPlan, Gym).join(
            Member, Member.id == MemberSubscription.member_id
        ).join(
            SubscriptionPlan, SubscriptionPlan.id == MemberSubscription.plan_id
        ).join(
            Gym, Gym.id == MemberSubscription.gym_id
        ).where(
            MemberSubscription.end_date == target_date,
            MemberSubscription.status == "active",
            MemberSubscription.reminder_sent == False
        )
        
        result = await session.execute(q)
        rows = result.all()
        
        sent_count = 0
        for sub, member, plan, gym in rows:
            success = await send_whatsapp_template(
                phone=member.phone,
                template_name=settings.WA_TEMPLATE_REMINDER,
                params=[member.name, plan.name, gym.name, str(sub.end_date)]
            )
            if success:
                sub.reminder_sent = True
                sent_count += 1
        
        await session.commit()
        return sent_count

@app.task(name="app.tasks.reminders.run")
def run():
    loop = asyncio.get_event_loop()
    count = loop.run_until_complete(send_reminders_task())
    return f"Sent {count} reminders"
