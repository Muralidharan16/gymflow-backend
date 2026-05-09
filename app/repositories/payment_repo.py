import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from sqlalchemy import select, func
from app.repositories.base import BaseRepository
from app.models.payment import Payment, Invoice

class PaymentRepository(BaseRepository[Payment]):
    def __init__(self, session):
        super().__init__(Payment, session)

    async def sum_by_date_range(self, gym_id: uuid.UUID, start: date, end: date) -> Decimal:
        q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.gym_id == gym_id,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
            Payment.status == "completed"
        )
        result = await self.session.execute(q)
        return result.scalar_one()

    async def get_yesterday_collections(self, gym_id: uuid.UUID) -> dict:
        today = date.today()
        yesterday = today - timedelta(days=1)
        q = select(
            Payment.payment_method,
            func.sum(Payment.amount)
        ).where(
            Payment.gym_id == gym_id,
            func.date(Payment.payment_date) == yesterday,
            Payment.status == "completed"
        ).group_by(Payment.payment_method)
        result = await self.session.execute(q)
        rows = result.all()
        breakdown = {row[0]: row[1] for row in rows}
        total = sum(breakdown.values(), Decimal(0))
        return {"total": total, "breakdown": breakdown}


class InvoiceRepository(BaseRepository[Invoice]):
    def __init__(self, session):
        super().__init__(Invoice, session)

    async def next_sequence_for_gym(self, gym_id: uuid.UUID) -> int:
        """Return the next daily invoice sequence number for a gym."""
        today = date.today()
        q = (
            select(Invoice.invoice_number)
            .where(
                Invoice.gym_id == gym_id,
                func.date(Invoice.issued_at) == today
            )
            .order_by(Invoice.invoice_number.desc())
            .limit(1)
        )
        result = await self.session.execute(q)
        last_inv_number = result.scalar_one_or_none()
        if last_inv_number:
            try:
                # invoice_number format: GYM001-2026-00005
                return int(last_inv_number.split("-")[-1]) + 1
            except (IndexError, ValueError):
                return 1
        return 1