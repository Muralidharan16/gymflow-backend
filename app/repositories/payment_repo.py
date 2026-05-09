import uuid
from typing import Optional, List
from datetime import datetime, date, timedelta
from sqlalchemy import select, func
from decimal import Decimal
from app.models.payment import Payment, Invoice
from app.repositories.base import BaseRepository

class PaymentRepository(BaseRepository[Payment]):
    def __init__(self, session):
        super().__init__(Payment, session)

    async def sum_by_date_range(self, gym_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
        q = select(func.sum(self.model.amount - self.model.discount_amount)).where(
            self.model.gym_id == gym_id,
            self.model.payment_date >= start,
            self.model.payment_date <= end,
            self.model.status == "completed"
        )
        result = await self.session.execute(q)
        return result.scalar_one() or Decimal("0")

    async def get_yesterday_collections(self, gym_id: uuid.UUID) -> Decimal:
        yesterday = date.today() - timedelta(days=1)
        start = datetime.combine(yesterday, datetime.min.time())
        end = datetime.combine(yesterday, datetime.max.time())
        return await self.sum_by_date_range(gym_id, start, end)

class InvoiceRepository(BaseRepository[Invoice]):
    def __init__(self, session):
        super().__init__(Invoice, session)

    async def next_sequence_for_gym(self, gym_id: uuid.UUID) -> int:
        q = select(func.count()).where(self.model.gym_id == gym_id)
        result = await self.session.execute(q)
        return result.scalar_one() + 1
