import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict, Any

from celery import shared_task
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.gym import Gym
from app.models.member import Member
from app.models.attendance import AttendanceLog
from app.models.payment import Payment, PaymentStatus
from app.utils.pdf import generate_digest_pdf

logger = logging.getLogger(__name__)


async def generate_daily_digest_for_gym(gym: Gym, target_date: datetime) -> Dict[str, Any]:
    """
    Generate daily digest statistics for a single gym.
    
    Returns:
        Dictionary with stats for the day
    """
    start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    async with async_session_maker() as session:
        # Count check-ins for the day
        checkins_query = select(func.count(AttendanceLog.id)).where(
            AttendanceLog.gym_id == gym.id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= start_of_day,
            AttendanceLog.check_in_time <= end_of_day
        )
        checkins_result = await session.execute(checkins_query)
        checkins_count = checkins_result.scalar() or 0
        
        # Count unique members who checked in
        unique_members_query = select(func.count(AttendanceLog.member_id.distinct())).where(
            AttendanceLog.gym_id == gym.id,
            AttendanceLog.granted == True,
            AttendanceLog.check_in_time >= start_of_day,
            AttendanceLog.check_in_time <= end_of_day
        )
        unique_result = await session.execute(unique_members_query)
        unique_members = unique_result.scalar() or 0
        
        # Total revenue for the day (completed payments)
        revenue_query = select(func.sum(Payment.amount)).where(
            Payment.gym_id == gym.id,
            Payment.status == PaymentStatus.COMPLETED,
            Payment.payment_date >= start_of_day,
            Payment.payment_date <= end_of_day
        )
        revenue_result = await session.execute(revenue_query)
        revenue = revenue_result.scalar() or Decimal('0')
        
        # Count new members created today
        new_members_query = select(func.count(Member.id)).where(
            Member.gym_id == gym.id,
            Member.is_active == True,
            Member.created_at >= start_of_day,
            Member.created_at <= end_of_day
        )
        new_members_result = await session.execute(new_members_query)
        new_members = new_members_result.scalar() or 0
        
        return {
            "gym_name": gym.name,
            "date": target_date.strftime("%Y-%m-%d"),
            "checkins": checkins_count,
            "unique_members": unique_members,
            "new_members": new_members,
            "revenue": float(revenue),
            "revenue_formatted": f"₹{revenue:,.2f}"
        }


async def daily_digest_task() -> int:
    """
    Generate daily digest PDFs for all active gyms for yesterday.
    
    Returns:
        Number of gyms processed
    """
    yesterday = datetime.now() - timedelta(days=1)
    logger.info(f"Generating daily digest for {yesterday.strftime('%Y-%m-%d')}")
    
    async with async_session_maker() as session:
        # Get all active gyms
        gyms_query = select(Gym).where(Gym.is_active == True)
        gyms_result = await session.execute(gyms_query)
        gyms = gyms_result.scalars().all()
        
        processed_count = 0
        for gym in gyms:
            try:
                stats = await generate_daily_digest_for_gym(gym, yesterday)
                
                # Generate PDF
                pdf_bytes = generate_digest_pdf(
                    gym_name=stats["gym_name"],
                    date_str=stats["date"],
                    stats={
                        "Total Check-ins": stats["checkins"],
                        "Unique Members": stats["unique_members"],
                        "New Members": stats["new_members"],
                        "Revenue": stats["revenue_formatted"]
                    }
                )
                
                # In production, save PDF to storage (S3/minio)
                # For now, log that PDF was generated
                logger.info(f"Generated digest for {gym.name}: {len(pdf_bytes)} bytes")
                
                processed_count += 1
                
            except Exception as e:
                logger.error(f"Failed to generate digest for gym {gym.id}: {str(e)}")
                continue
        
        return processed_count


@shared_task(name="app.tasks.daily_digest.run")
def run():
    """
    Celery task entry point for daily digest generation.
    """
    try:
        count = asyncio.run(daily_digest_task())
        logger.info(f"Daily digest task completed: processed {count} gyms")
        return f"Processed {count} gyms"
    except Exception as e:
        logger.error(f"Daily digest task failed: {str(e)}")
        raise