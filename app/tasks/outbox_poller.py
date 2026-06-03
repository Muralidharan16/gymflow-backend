from datetime import datetime, timezone
import asyncio
from celery import shared_task
import logging

from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.outbox import TransactionalOutbox
from app.tasks.branch_hours_projection import run_projection

logger = logging.getLogger(__name__)

async def _poll_outbox():
    async with async_session_maker() as session:
        stmt = select(TransactionalOutbox).where(
            TransactionalOutbox.processed_at.is_(None),
            TransactionalOutbox.dead_lettered_at.is_(None),
            TransactionalOutbox.delivery_attempts < 15
        ).order_by(TransactionalOutbox.created_at).limit(100)
        
        events = (await session.scalars(stmt)).all()
        
        for event in events:
            try:
                if event.event_type == "branch_hours.changed":
                    branch_id = event.payload.get("branch_id")
                    if branch_id:
                        run_projection.delay(branch_id)
                
                event.processed_at = datetime.now(timezone.utc)
            except Exception as e:
                event.delivery_attempts += 1
                event.last_error = str(e)
                if event.delivery_attempts >= 15:
                    event.dead_lettered_at = datetime.now(timezone.utc)
                    logger.error(f"Outbox event {event.id} dead lettered: {e}")
        
        if events:
            await session.commit()

@shared_task(name="app.tasks.outbox_poller.run")
def run():
    asyncio.run(_poll_outbox())
