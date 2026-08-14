import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import worker_async_session_maker
from app.models.member import Member
from app.models.subscription import MemberSubscription, SubscriptionStatus
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.utils.whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)


@shared_task(name="send_daily_reminders")
def send_daily_reminders():
    try:
        asyncio.run(_send_daily_reminders_async())
    except Exception as e:
        logger.error(f"Failed to send daily reminders: {str(e)}")
        raise


async def _send_daily_reminders_async():
    async with worker_async_session_maker() as session:
        try:
            await _send_expired_reminders(session)
            await _send_frozen_expiry_reminders(session)
            await _send_inactivity_reminders(session)
            await session.commit()
            logger.info("Daily reminders sent successfully")
        except Exception as e:
            await session.rollback()
            logger.error(f"Error in _send_daily_reminders_async: {str(e)}")
            raise


async def _send_expired_reminders(session):
    repo = SubscriptionRepository(session)
    today = datetime.now(timezone.utc).date()
    three_days_ago = today - timedelta(days=3)
    expired_subs = await repo.get_expired_subscriptions_in_range(three_days_ago, today)
    for sub in expired_subs:
        member = sub.member
        if not member or not member.phone:
            if member:
                logger.warning(f"Member {member.id} has no phone, skipping expired reminder")
            continue
        has_active = await repo.has_active_subscription(member.id, member.gym_id)
        if has_active:
            continue
        plan = sub.plan
        plan_name = plan.name if plan else "membership"
        message = f"⚠️ *Doers Gym - Membership Expired*\n\n"
        message += f"Dear {member.name},\n"
        message += f"Your {plan_name} expired on {sub.end_date.strftime('%d %b %Y')}.\n"
        message += f"Please renew your membership to continue using the gym.\n\n"
        message += f"Contact us or visit the reception for renewal options.\n"
        message += f"📞 +91-XXXXXXXXXX"
        try:
            await send_whatsapp_message(member.phone, message)
            logger.info(f"Sent expired reminder to {member.phone}")
        except Exception as e:
            logger.error(f"Failed to send expired reminder to {member.phone}: {str(e)}")


async def _send_frozen_expiry_reminders(session):
    repo = SubscriptionRepository(session)
    today = datetime.now(timezone.utc).date()
    three_days_from_now = today + timedelta(days=3)
    frozen_subs = await repo.get_frozen_subscriptions_ending_on(three_days_from_now)
    for sub in frozen_subs:
        member = sub.member
        if not member or not member.phone:
            if member:
                logger.warning(f"Member {member.id} has no phone, skipping frozen expiry reminder")
            continue
        plan = sub.plan
        plan_name = plan.name if plan else "membership"
        remaining_days = (sub.end_date - today).days
        message = f"❄️ *Doers Gym - Frozen Membership Expiring*\n\n"
        message += f"Hello {member.name},\n"
        message += f"Your frozen {plan_name} will expire in {remaining_days} day(s) on {sub.end_date.strftime('%d %b %Y')}.\n"
        message += f"To reactivate, please visit the gym or contact us.\n\n"
        message += f"Note: Unused frozen days will be lost after expiry."
        try:
            await send_whatsapp_message(member.phone, message)
            logger.info(f"Sent frozen expiry reminder to {member.phone}")
        except Exception as e:
            logger.error(f"Failed to send frozen reminder to {member.phone}: {str(e)}")


async def _send_inactivity_reminders(session):
    member_repo = MemberRepository(session)
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    inactive_members = await member_repo.get_inactive_members_with_active_subscription(seven_days_ago)
    for member in inactive_members:
        if not member.phone:
            logger.warning(f"Member {member.id} has no phone, skipping inactivity reminder")
            continue
        message = f"🏋️ *Doers Gym - We Miss You!*\n\n"
        message += f"Hi {member.name},\n"
        message += f"We noticed you haven't visited the gym in the last 7 days.\n"
        message += f"Your active membership is waiting for you! Come back and crush your fitness goals.\n\n"
        message += f"💪 See you soon at Doers Gym!"
        try:
            await send_whatsapp_message(member.phone, message)
            logger.info(f"Sent inactivity reminder to {member.phone}")
        except Exception as e:
            logger.error(f"Failed to send inactivity reminder to {member.phone}: {str(e)}")


@shared_task(name="send_birthday_wishes")
def send_birthday_wishes():
    try:
        asyncio.run(_send_birthday_wishes_async())
    except Exception as e:
        logger.error(f"Failed to send birthday wishes: {str(e)}")
        raise


async def _send_birthday_wishes_async():
    async with worker_async_session_maker() as session:
        try:
            member_repo = MemberRepository(session)
            today = datetime.now(timezone.utc).date()
            birthday_members = await member_repo.get_members_with_birthday(today)
            for member in birthday_members:
                if not member.phone:
                    logger.warning(f"Member {member.id} has no phone, skipping birthday wish")
                    continue
                message = f"🎂 *Happy Birthday, {member.name}!* 🎉\n\n"
                message += f"Wishing you a fantastic birthday from the entire Doers Gym team!\n"
                message += f"May this year bring you strength, health, and happiness.\n\n"
                message += f"🎁 Come by the gym today for a special birthday workout session!\n"
                message += f"#DoersGym #BirthdayWorkout"
                try:
                    await send_whatsapp_message(member.phone, message)
                    logger.info(f"Sent birthday wish to {member.phone}")
                except Exception as e:
                    logger.error(f"Failed to send birthday wish to {member.phone}: {str(e)}")
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Error in _send_birthday_wishes_async: {str(e)}")
            raise
