import uuid
from typing import List
from datetime import date, datetime, timedelta
from decimal import Decimal
from sqlalchemy import select, func
from app.repositories.base import BaseRepository
from app.models.payment import Payment, Invoice

class PaymentRepository(BaseRepository[Payment]):
    def __init__(self, session):
        super().__init__(Payment, session)

    async def sum_by_date_range(self, gym_id: uuid.UUID, start: date, end: date) -> Decimal:
        """Tenant-safe collection sum."""
        q = select(func.coalesce(func.sum(self.model.amount), 0)).where(
            self.model.gym_id == gym_id,
            self.model.payment_date >= start,
            self.model.payment_date <= end,
            self.model.status == "completed"
        )
        result = await self.session.execute(q)
        return await result.scalar_one()

    async def get_yesterday_collections(self, gym_id: uuid.UUID) -> dict:
        """Tenant-safe collection breakdown for yesterday."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        q = select(
            self.model.payment_method,
            func.sum(self.model.amount)
        ).where(
            self.model.gym_id == gym_id,
            func.date(self.model.payment_date) == yesterday,
            self.model.status == "completed"
        ).group_by(self.model.payment_method)
        result = await self.session.execute(q)
        rows = result.all()
        breakdown = {row[0]: row[1] for row in rows}
        total = sum(breakdown.values(), Decimal(0))
        return {"total": total, "breakdown": breakdown}

    async def list_by_member(self, member_id: uuid.UUID, gym_id: uuid.UUID) -> List[Payment]:
        """Tenant-safe list of payments for a member."""
        q = select(self.model).where(
            self.model.member_id == member_id,
            self.model.gym_id == gym_id
        ).order_by(self.model.payment_date.desc())
        result = await self.session.execute(q)
        return list(result.scalars().all())


class InvoiceRepository(BaseRepository[Invoice]):
    def __init__(self, session):
        super().__init__(Invoice, session)

    async def next_sequence_for_gym(self, gym_id: uuid.UUID) -> int:
        """
        Return the next daily invoice sequence number for a gym.
        Uses row-level locking to prevent duplicates under high concurrency.
        """
        today = date.today()
        # Lock the most recent invoice for this gym today to ensure sequence integrity
        q = (
            select(self.model.invoice_number)
            .where(
                self.model.gym_id == gym_id,
                func.date(self.model.issued_at) == today
            )
            .order_by(self.model.invoice_number.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self.session.execute(q)
        last_inv_number = await result.scalar_one_or_none()
        
        if last_inv_number:
            try:
                # Expected format: INV-GYM-YYYYMMDD-XXXX
                return int(last_inv_number.split("-")[-1]) + 1
            except (IndexError, ValueError):
                return 1
        return 1