from datetime import datetime, timezone, timedelta
from celery import shared_task
import asyncio
import logging

from sqlalchemy import text
from app.core.database import async_session_maker

logger = logging.getLogger(__name__)

async def _create_next_month_audit_partition():
    """
    Creates the branch_hours_audit_log partition for the upcoming month.
    Designed to run around the 25th of the month.
    """
    now = datetime.now(timezone.utc)
    # Target next month
    next_month_date = now.replace(day=28) + timedelta(days=4)
    target_year = next_month_date.year
    target_month = next_month_date.month
    
    start_date = datetime(target_year, target_month, 1)
    if target_month == 12:
        end_date = datetime(target_year + 1, 1, 1)
    else:
        end_date = datetime(target_year, target_month + 1, 1)
        
    partition_name = f"branch_hours_audit_log_y{target_year}m{target_month:02d}"
    
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    
    async with async_session_maker() as session:
        try:
            # Create partition
            await session.execute(text(f"""
                CREATE TABLE IF NOT EXISTS public.{partition_name} 
                PARTITION OF public.branch_hours_audit_log 
                FOR VALUES FROM ('{start_str}') TO ('{end_str}');
            """))
            
            # Enforce RLS inheritance explicitly
            await session.execute(text(f"ALTER TABLE public.{partition_name} ENABLE ROW LEVEL SECURITY;"))
            await session.execute(text(f"ALTER TABLE public.{partition_name} FORCE ROW LEVEL SECURITY;"))
            
            await session.commit()
            logger.info(f"Successfully provisioned audit partition {partition_name} for {start_str} to {end_str}.")
        except Exception as e:
            await session.rollback()
            logger.error(f"Failed to create audit partition {partition_name}: {e}")
            raise

async def ensure_audit_partitions():
    """
    Startup bootstrapper: Ensures branch_hours_audit_log partitions exist for 
    the current month and next month to prevent insert failures on cold starts.
    """
    now = datetime.now(timezone.utc)
    
    # Calculate current and next month dates
    current_date = now.replace(day=1)
    next_date = (current_date + timedelta(days=32)).replace(day=1)
    
    months_to_provision = [current_date, next_date]
    
    async with async_session_maker() as session:
        for target_date in months_to_provision:
            target_year = target_date.year
            target_month = target_date.month
            
            start_date = datetime(target_year, target_month, 1)
            if target_month == 12:
                end_date = datetime(target_year + 1, 1, 1)
            else:
                end_date = datetime(target_year, target_month + 1, 1)
                
            partition_name = f"branch_hours_audit_log_y{target_year}m{target_month:02d}"
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            
            try:
                # We use IF NOT EXISTS to make it safely idempotent
                await session.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS public.{partition_name} 
                    PARTITION OF public.branch_hours_audit_log 
                    FOR VALUES FROM ('{start_str}') TO ('{end_str}');
                """))
                
                await session.execute(text(f"ALTER TABLE public.{partition_name} ENABLE ROW LEVEL SECURITY;"))
                await session.execute(text(f"ALTER TABLE public.{partition_name} FORCE ROW LEVEL SECURITY;"))
                
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"Failed to bootstrap audit partition {partition_name}: {e}")
                raise

@shared_task(name="app.tasks.branch_hours_partition.run")
def run():
    asyncio.run(_create_next_month_audit_partition())

