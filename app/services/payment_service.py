import logging
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment, PaymentStatus, PaymentMethod, PaymentType, Invoice
from app.repositories.payment_repo import PaymentRepository, InvoiceRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.gym_repo import GymRepository
from app.services.invoice_service import InvoiceService
from app.core.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)


class PaymentService:
    """Service for managing payments and invoices."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.subscription_repo = SubscriptionRepository(session)
        self.member_repo = MemberRepository(session)
        self.gym_repo = GymRepository(session)

    async def create_payment(
        self,
        gym_id: UUID,
        member_id: UUID,
        subscription_id: Optional[UUID],
        amount: Decimal,
        method: PaymentMethod,
        type: PaymentType,
        reference_number: Optional[str],
        notes: Optional[str],
        created_by: UUID,
    ) -> Payment:
        """PAY-15: legacy payment mutation authority is permanently retired."""
        del (
            gym_id,
            member_id,
            subscription_id,
            amount,
            method,
            type,
            reference_number,
            notes,
            created_by,
        )
        raise RuntimeError(
            "PAY-15 legacy payment writes are retired; use Finance Core"
        )

    async def get_payment(self, payment_id: UUID, gym_id: UUID) -> Payment:
        """
        Get a single payment by ID, scoped to gym.
        
        Args:
            payment_id: Payment UUID
            gym_id: Gym UUID for access control
            
        Returns:
            Payment object
            
        Raises:
            NotFoundError: If payment not found or doesn't belong to gym
        """
        payment = await self.payment_repo.get_by_id_and_gym(payment_id, gym_id)
        if not payment:
            raise NotFoundError(f"Payment {payment_id} not found in gym {gym_id}", error_code="NOT_FOUND")
        return payment

    async def list_payments(
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
        return await self.payment_repo.list_filtered(
            gym_id, date_from, date_to, method, type, status, page, size
        )

    async def get_invoice_for_payment(self, payment_id: UUID, gym_id: UUID) -> Invoice:
        """
        Get the invoice associated with a payment.
        
        Args:
            payment_id: Payment UUID
            gym_id: Gym UUID for access control
            
        Returns:
            Invoice object
            
        Raises:
            NotFoundError: If payment or invoice not found
        """
        payment = await self.get_payment(payment_id, gym_id)
        invoice = await self.payment_repo.get_invoice_for_payment(payment_id)
        if not invoice:
            raise NotFoundError(f"No invoice found for payment {payment_id}", error_code="NOT_FOUND")
        return invoice

    async def get_payment_summary_by_method(
        self,
        gym_id: UUID,
        date_from: date,
        date_to: date
    ) -> List[dict]:
        """
        Get payment summary grouped by payment method for reports.
        """
        return await self.payment_repo.get_summary_by_method(gym_id, date_from, date_to)

    async def get_total_revenue(
        self,
        gym_id: UUID,
        date_from: date,
        date_to: date
    ) -> Decimal:
        """
        Get total revenue for a date range.
        """
        return await self.payment_repo.get_total_revenue(gym_id, date_from, date_to)