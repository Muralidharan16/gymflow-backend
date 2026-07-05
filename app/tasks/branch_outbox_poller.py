import asyncio
import logging
from datetime import datetime, timezone, timedelta
from celery import shared_task
from sqlalchemy import text

from app.core.database import async_session_maker

logger = logging.getLogger(__name__)

async def _poll_branch_outbox():
    """
    Transactional Outbox processor for Branch Lifecycle Events.
    Implements Section 7.2 (Processor Claim Query) and 7.3 (Stuck-Event Rescue).
    """
    async with async_session_maker() as session:
        now = datetime.now(timezone.utc)
        
        # 1. Stuck-Event Rescue (Section 7.3)
        # Resets stuck events to pending. Does NOT increment attempt_count.
        try:
            await session.execute(
                text("""
                    UPDATE public.branch_outbox_events
                    SET status = 'pending',
                        last_attempted_at = NULL
                    WHERE status = 'processing'
                      AND last_attempted_at < :rescue_threshold;
                """),
                {"rescue_threshold": now - timedelta(minutes=15)}
            )
            await session.commit()
        except Exception as e:
            logger.error(f"Error rescuing stuck branch outbox events: {e}")
            await session.rollback()

        # 2. Claim pending events (Section 7.2)
        try:
            claim_stmt = text("""
                UPDATE public.branch_outbox_events
                SET status = 'processing',
                    last_attempted_at = :now,
                    attempt_count = attempt_count + 1
                WHERE outbox_id IN (
                    SELECT outbox_id FROM public.branch_outbox_events
                    WHERE status = 'pending'
                    AND process_after <= :now
                    AND attempt_count < max_attempts
                    ORDER BY process_after, created_at
                    LIMIT 100
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING outbox_id, branch_id, event_type, payload, attempt_count, max_attempts;
            """)
            
            res = await session.execute(claim_stmt, {"now": now})
            claimed_events = res.fetchall()
            await session.commit()
            
            for event in claimed_events:
                outbox_id, branch_id, event_type, payload, attempt_count, max_attempts = event
                
                try:
                    # Process based on event_type
                    if event_type == "branch.search_deindex":
                        # Perform sync/async de-indexing logic
                        logger.info(f"De-indexing branch {branch_id}")
                        # Mock: await search_service.deindex_branch(branch_id)
                        
                    elif event_type == "branch.search_index":
                        # Perform sync/async indexing logic
                        logger.info(f"Indexing branch {branch_id}")
                        # Mock: await search_service.index_branch(branch_id)
                        
                    elif event_type == "branch.member_notification":
                        # Perform member notification
                        logger.info(f"Notifying members for branch {branch_id} transition")
                        # Mock: await notification_service.send_branch_transition_notice(payload)
                        
                    else:
                        logger.warning(f"Unknown branch outbox event type: {event_type}")

                    # Mark as delivered
                    await session.execute(
                        text("UPDATE public.branch_outbox_events SET status = 'delivered' WHERE outbox_id = :id"),
                        {"id": outbox_id}
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to process outbox event {outbox_id}: {e}")
                    
                    # Classification and error handling
                    new_status = 'pending'
                    if attempt_count >= max_attempts:
                        new_status = 'dead_lettered'
                        
                    # Here we could check for specific exceptions like schema mismatch -> compatibility_queue
                    # or data corruption -> quarantined
                    
                    await session.execute(
                        text("""
                            UPDATE public.branch_outbox_events 
                            SET status = :status, last_error = :err 
                            WHERE outbox_id = :id
                        """),
                        {"status": new_status, "err": str(e), "id": outbox_id}
                    )
                    
            if claimed_events:
                await session.commit()

        except Exception as e:
            logger.error(f"Error claiming branch outbox events: {e}")
            await session.rollback()

@shared_task(name="app.tasks.branch_outbox_poller.run")
def run():
    asyncio.run(_poll_branch_outbox())
