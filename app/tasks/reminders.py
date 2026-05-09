import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from celery import shared_task

from app.core.database import async_session_maker
from app.models.member import Member
from app.models.member_subscription import MemberSubscription
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.utils.whatsapp import send_whatsapp_message
from app.core.logging import logger


@shared_task(name="send_daily_reminders")
def send_daily_reminders():
    """
    Send daily reminders to members with overdue payments or expiring soon.
    """
    try:
        asyncio.run(_send_daily_reminders_async())
    except Exception as e:
        logger.error(f"Failed to send daily reminders: {str(e)}")
        raise


async def _send_daily_reminders_async():
    """Async implementation for daily reminders."""
    async with async_session_maker() as session:
        try:
            # Reminder types:
            # 1. Members with expired subscriptions (grace period reminder)
            # 2. Members with frozen subscriptions expiring soon
            # 3. Members with no check-in for > 7 days (engagement reminder)
            
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
    """Send reminders to members whose subscriptions expired in last 3 days."""
    repo = SubscriptionRepository(session)
    today = datetime.now(timezone.utc).date()
    three_days_ago = today - timedelta(days=3)
    
    # Get subscriptions that expired between 3 days ago and today
    expired_subs = await repo.get_expired_subscriptions_in_range(three_days_ago, today)
    
    for sub in expired_subs:
        member = sub.member
        if not member or not member.phone:
            continue
        
        # Skip if already sent reminder for this subscription today (track in separate table? 
        # For simplicity, we can check if member has any active subscription now)
        # Avoid spamming: only send if no active subscription exists
        has_active = await repo.has_active_subscription(member.id)
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
    """Send reminders to members with frozen subscriptions that expire in 3 days."""
    repo = SubscriptionRepository(session)
    today = datetime.now(timezone.utc).date()
    three_days_from_now = today + timedelta(days=3)
    
    frozen_subs = await repo.get_frozen_subscriptions_ending_on(three_days_from_now)
    
    for sub in frozen_subs:
        member = sub.member
        if not member or not member.phone:
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
    """Send reminders to members who haven't checked in for 7+ days."""
    member_repo = MemberRepository(session)
    from datetime import timedelta
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    
    # Get members with no attendance in last 7 days AND active subscription
    inactive_members = await member_repo.get_inactive_members_with_active_subscription(seven_days_ago)
    
    for member in inactive_members:
        if not member.phone:
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
    """
    Send birthday wishes to members whose birthday is today.
    """
    try:
        asyncio.run(_send_birthday_wishes_async())
    except Exception as e:
        logger.error(f"Failed to send birthday wishes: {str(e)}")
        raise


async def _send_birthday_wishes_async():
    """Async implementation for birthday wishes."""
    async with async_session_maker() as session:
        try:
            member_repo = MemberRepository(session)
            today = datetime.now(timezone.utc).date()
            
            # Get members with date_of_birth matching today (month and day)
            birthday_members = await member_repo.get_members_with_birthday(today)
            
            for member in birthday_members:
                if not member.phone:
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