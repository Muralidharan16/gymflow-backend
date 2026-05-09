import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.subscription import MemberSubscription, SubscriptionStatus
from app.repositories.subscription_repo import SubscriptionRepository
from app.services.subscription_service import SubscriptionService
from app.utils.whatsapp import send_whatsapp_message

logger = logging.getLogger(__name__)


@shared_task(name="expire_subscriptions")
def expire_subscriptions():
    """
    Celery task to expire subscriptions where end_date < today and status is active.
    Runs daily via Celery beat.
    """
    try:
        asyncio.run(_expire_subscriptions_async())
    except Exception as e:
        logger.error(f"Failed to expire subscriptions: {str(e)}")
        raise


async def _expire_subscriptions_async():
    """Async implementation of subscription expiration."""
    async with async_session_maker() as session:
        try:
            subscription_service = SubscriptionService(session)
            
            # Get all active subscriptions that have ended
            repo = SubscriptionRepository(session)
            today = datetime.now(timezone.utc).date()
            
            # Query active subscriptions with end_date < today
            expired_subs = await repo.get_expired_active_subscriptions(today)
            
            expired_count = 0
            for sub in expired_subs:
                try:
                    await subscription_service.expire_subscription(sub.id)
                    expired_count += 1
                    logger.info(f"Expired subscription {sub.id} for member {sub.member_id}")
                except Exception as e:
                    logger.error(f"Failed to expire subscription {sub.id}: {str(e)}")
                    continue
            
            await session.commit()
            logger.info(f"Expired {expired_count} subscriptions")
            
            # Send reminders for subscriptions expiring in 3 days
            await _send_expiry_reminders(session)
            
        except Exception as e:
            await session.rollback()
            logger.error(f"Error in _expire_subscriptions_async: {str(e)}")
            raise


async def _send_expiry_reminders(session: AsyncSession):
    """
    Send WhatsApp reminders for subscriptions expiring in 3 days.
    """
    try:
        repo = SubscriptionRepository(session)
        today = datetime.now(timezone.utc).date()
        target_date = today + timedelta(days=3)
        
        # Need to implement this method in subscription_repo if not exists
        # Fallback: manual query
        from sqlalchemy import select
        query = select(MemberSubscription).where(
            MemberSubscription.status == SubscriptionStatus.ACTIVE,
            MemberSubscription.end_date == target_date
        )
        result = await session.execute(query)
        expiring_subs = result.scalars().all()
        
        for sub in expiring_subs:
            member = sub.member
            if not member or not member.phone:
                continue
            
            plan = sub.plan
            plan_name = plan.name if plan else "Unknown"
            
            message = f"🔔 *Doers Gym Reminder*\n\n"
            message += f"Dear {member.name},\n"
            message += f"Your subscription '{plan_name}' will expire on {sub.end_date.strftime('%d %b %Y')}.\n"
            message += f"Please renew your membership to continue enjoying our services.\n\n"
            message += f"Visit the gym or contact us for renewal options."
            
            try:
                await send_whatsapp_message(member.phone, message)
                logger.info(f"Sent expiry reminder to {member.phone} for sub {sub.id}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {member.phone}: {str(e)}")
                
    except Exception as e:
        logger.error(f"Error sending expiry reminders: {str(e)}")


@shared_task(name="check_expiring_soon")
def check_expiring_soon():
    """
    Additional task to send reminders for subscriptions expiring in next 7 days.
    This can be run daily as well.
    """
    try:
        asyncio.run(_check_expiring_soon_async())
    except Exception as e:
        logger.error(f"Failed to check expiring soon: {str(e)}")
        raise


async def _check_expiring_soon_async():
    """Async implementation for checking soon-to-expire subscriptions."""
    async with async_session_maker() as session:
        try:
            from sqlalchemy import select
            from app.models.subscription import MemberSubscription, SubscriptionStatus
            today = datetime.now(timezone.utc).date()
            future_date = today + timedelta(days=7)
            
            query = select(MemberSubscription).where(
                MemberSubscription.status == SubscriptionStatus.ACTIVE,
                MemberSubscription.end_date >= today,
                MemberSubscription.end_date <= future_date
            )
            result = await session.execute(query)
            expiring_subs = result.scalars().all()
            
            for sub in expiring_subs:
                member = sub.member
                if not member or not member.phone:
                    continue
                
                days_left = (sub.end_date - today).days
                if days_left <= 0:
                    continue
                
                plan = sub.plan
                plan_name = plan.name if plan else "Unknown"
                
                message = f"🌟 *Doers Gym - Subscription Reminder*\n\n"
                message += f"Hi {member.name},\n"
                message += f"Your '{plan_name}' plan expires in {days_left} day(s) on {sub.end_date.strftime('%d %b %Y')}.\n"
                message += f"Renew now to avoid interruption. Contact reception for assistance.\n\n"
                message += f"Thank you for being a valued member!"
                
                try:
                    await send_whatsapp_message(member.phone, message)
                    logger.info(f"Sent early reminder to {member.phone} for sub {sub.id}")
                except Exception as e:
                    logger.error(f"Failed to send early reminder: {str(e)}")
            
            await session.commit()
            
        except Exception as e:
            await session.rollback()
            logger.error(f"Error in _check_expiring_soon_async: {str(e)}")
            raise