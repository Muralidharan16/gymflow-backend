from datetime import date, datetime
from typing import List, Optional, Tuple
from uuid import UUID
from decimal import Decimal

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.payment import Payment, PaymentStatus, PaymentMethod, PaymentType
from app.models.invoice import Invoice
from app.repositories.base_repo import BaseRepository


class PaymentRepository(BaseRepository[Payment]):
    """Repository for Payment operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Payment, session)

    async def get_by_id(self, payment_id: UUID) -> Optional[Payment]:
        """Get payment by ID with eager loading."""
        query = select(Payment).where(
            Payment.id == payment_id
        ).options(
            selectinload(Payment.gym),
            selectinload(Payment.member),
            selectinload(Payment.created_by_staff)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id_and_gym(self, payment_id: UUID, gym_id: UUID) -> Optional[Payment]:
        """Get payment by ID scoped to gym."""
        query = select(Payment).where(
            Payment.id == payment_id,
            Payment.gym_id == gym_id
        ).options(
            selectinload(Payment.member),
            selectinload(Payment.created_by_staff)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, payment: Payment) -> Payment:
        """Create a new payment."""
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def update(self, payment: Payment) -> Payment:
        """Update an existing payment."""
        await self.session.merge(payment)
        await self.session.flush()
        return payment

    async def list_filtered(
        self,
        gym_id: UUID,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        method: Optional[PaymentMethod] = None,
        type: Optional[PaymentType] = None,
        status: Optional[PaymentStatus] = None,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[Payment], int]:
        """
        List payments with filters and pagination.
        
        Args:
            gym_id: Gym UUID
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            method: Payment method filter
            type: Payment type filter
            status: Payment status filter
            page: Page number (1-indexed)
            size: Items per page
            
        Returns:
            Tuple of (list of payments, total count)
        """
        offset = (page - 1) * size
        
        # Base query
        query = select(Payment).where(Payment.gym_id == gym_id)
        
        # Apply date filters
        if date_from:
            from_dt = datetime.combine(date_from, datetime.min.time())
            query = query.where(Payment.payment_date >= from_dt)
        
        if date_to:
            to_dt = datetime.combine(date_to, datetime.max.time())
            query = query.where(Payment.payment_date <= to_dt)
        
        # Apply other filters
        if method:
            query = query.where(Payment.method == method)
        
        if type:
            query = query.where(Payment.type == type)
        
        if status:
            query = query.where(Payment.status == status)
        
        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        
        # Get paginated results with eager loading
        query = query.options(
            selectinload(Payment.member),
            selectinload(Payment.created_by_staff)
        ).order_by(Payment.payment_date.desc()).offset(offset).limit(size)
        
        result = await self.session.execute(query)
        payments = result.scalars().all()
        
        return payments, total

    async def get_invoice_for_payment(self, payment_id: UUID) -> Optional[Invoice]:
        """Get invoice associated with a payment."""
        query = select(Invoice).where(Invoice.payment_id == payment_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_summary_by_method(
        self,
        gym_id: UUID,
        date_from: date,
        date_to: date
    ) -> List[dict]:
        """
        Get payment summary grouped by payment method for reports.
        
        Returns:
            List of dicts with method, total_amount, count
        """
        from_dt = datetime.combine(date_from, datetime.min.time())
        to_dt = datetime.combine(date_to, datetime.max.time())
        
        query = select(
            Payment.method,
            func.sum(Payment.amount).label('total_amount'),
            func.count().label('count')
        ).where(
            Payment.gym_id == gym_id,
            Payment.status == PaymentStatus.COMPLETED,
            Payment.payment_date >= from_dt,
            Payment.payment_date <= to_dt
        ).group_by(Payment.method)
        
        result = await self.session.execute(query)
        rows = result.all()
        
        return [
            {
                "method": row.method.value if row.method else None,
                "total_amount": float(row.total_amount) if row.total_amount else 0.0,
                "count": row.count
            }
            for row in rows
        ]

    async def get_total_revenue(
        self,
        gym_id: UUID,
        date_from: date,
        date_to: date
    ) -> Decimal:
        """Get total revenue for a date range (completed payments only)."""
        from_dt = datetime.combine(date_from, datetime.min.time())
        to_dt = datetime.combine(date_to, datetime.max.time())
        
        query = select(func.sum(Payment.amount)).where(
            Payment.gym_id == gym_id,
            Payment.status == PaymentStatus.COMPLETED,
            Payment.payment_date >= from_dt,
            Payment.payment_date <= to_dt
        )
        result = await self.session.execute(query)
        total = result.scalar()
        return total or Decimal('0')

    async def get_payments_by_subscription(self, subscription_id: UUID) -> List[Payment]:
        """Get all payments linked to a specific subscription."""
        query = select(Payment).where(
            Payment.subscription_id == subscription_id
        ).order_by(Payment.payment_date)
        result = await self.session.execute(query)
        return result.scalars().all()