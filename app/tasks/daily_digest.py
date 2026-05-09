from app.tasks.celery_app import app
from app.core.database import AsyncSessionLocal
from app.models.gym import Gym, GymOwner
from app.models.payment import Payment
from app.models.member import Member
from app.models.subscription import MemberSubscription
from app.utils.pdf import generate_digest_pdf
from app.utils.whatsapp import send_whatsapp_document
from sqlalchemy import select, func
from datetime import date, timedelta, datetime
from decimal import Decimal
import asyncio


async def daily_digest_task():
    async with AsyncSessionLocal() as session:
        today = date.today()
        yesterday = today - timedelta(days=1)
        start_dt = datetime.combine(yesterday, datetime.min.time())
        end_dt = datetime.combine(yesterday, datetime.max.time())

        # Get active gyms
        q_gyms = select(Gym).where(Gym.is_active == True)
        gyms = (await session.execute(q_gyms)).scalars().all()

        gyms_processed = 0
        for gym in gyms:
            # 1. Collections breakdown
            q_pay = select(Payment.payment_method, func.sum(Payment.amount)).where(
                Payment.gym_id == gym.id,
                Payment.payment_date >= start_dt,
                Payment.payment_date <= end_dt,
                Payment.status == "completed"
            ).group_by(Payment.payment_method)
            res_pay = (await session.execute(q_pay)).all()
            breakdown = {row[0]: row[1] for row in res_pay}
            total_coll = sum(breakdown.values(), Decimal(0))

            # 2. Expired yesterday
            q_exp = select(Member.name, Member.phone, MemberSubscription.plan_id).join(
                MemberSubscription, Member.id == MemberSubscription.member_id
            ).where(
                MemberSubscription.gym_id == gym.id,
                MemberSubscription.end_date == yesterday
            )
            res_exp = (await session.execute(q_exp)).all()
            # Fetch plan names (simplified: just IDs or another query)
            expired_members = [{"name": r[0], "phone": r[1], "plan": str(r[2])} for r in res_exp]

            # 3. Expiring soon (today + tomorrow)
            q_soon = select(Member.name, Member.phone, MemberSubscription.plan_id, MemberSubscription.end_date).join(
                MemberSubscription, Member.id == MemberSubscription.member_id
            ).where(
                MemberSubscription.gym_id == gym.id,
                MemberSubscription.end_date.in_([today, today + timedelta(days=1)])
            )
            res_soon = (await session.execute(q_soon)).all()
            expiring_soon = [{"name": r[0], "phone": r[1], "plan": str(r[2]), "end_date": r[3]} for r in res_soon]

            # Generate PDF
            pdf_bytes = generate_digest_pdf(
                gym.name,
                total_coll,
                breakdown,
                expired_members,
                expiring_soon
            )

            # Send to owner
            q_owner = select(GymOwner).where(GymOwner.gym_id == gym.id, GymOwner.role == "owner")
            owner = (await session.execute(q_owner)).scalar_one_or_none()

            if owner and owner.phone:
                # In real scenario, upload bytes to S3 and get URL
                # For now, we mock it.
                pdf_url = "https://example.com/mock_report.pdf" 
                await send_whatsapp_document(owner.phone, pdf_url, f"Daily_Report_{gym.name}_{today}.pdf")
                gyms_processed += 1

        return gyms_processed


@app.task(name="app.tasks.daily_digest.run")
def run():
    # Use asyncio.run for a clean execution in celery worker
    try:
        count = asyncio.run(daily_digest_task())
    except RuntimeError:
        # Loop might already be running in some environments
        loop = asyncio.get_event_loop()
        count = loop.run_until_complete(daily_digest_task())
    return f"Processed {count} gyms"
