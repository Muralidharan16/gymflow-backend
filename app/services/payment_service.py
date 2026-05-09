from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.payment import Payment, PaymentStatus, PaymentMethod, PaymentType
from app.models.invoice import Invoice
from app.repositories.payment_repo import PaymentRepository
from app.repositories.subscription_repo import SubscriptionRepository
from app.services.invoice_service import InvoiceService
from app.core.logging import logger


class PaymentService:
    """Service for managing payments and invoices."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.subscription_repo = SubscriptionRepository(session)

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
        created_by: UUID
    ) -> Payment:
        """
        Record a new payment and automatically generate invoice for completed payments.
        
        Args:
            gym_id: Gym UUID
            member_id: Member UUID
            subscription_id: Optional subscription UUID this payment is for
            amount: Payment amount
            method: Payment method (CASH, CARD, UPI, etc.)
            type: Payment type (SUBSCRIPTION, ADDON, etc.)
            reference_number: Optional transaction reference number
            notes: Optional notes
            created_by: Staff UUID recording payment
            
        Returns:
            Created Payment object
        """
        from app.repositories.member_repo import MemberRepository
        from app.repositories.gym_repo import GymRepository
        
        member_repo = MemberRepository(self.session)
        gym_repo = GymRepository(self.session)
        
        # Verify member exists in this gym
        member = await member_repo.get_by_id_active(member_id, gym_id)
        if not member:
            raise NotFoundError(f"Member {member_id} not found in gym {gym_id}")
        
        # Verify subscription if provided
        if subscription_id:
            sub = await self.subscription_repo.get_by_id(subscription_id)
            if not sub or sub.member_id != member_id:
                raise NotFoundError(f"Subscription {subscription_id} not found for member {member_id}")
        
        # Create payment
        payment = Payment(
            gym_id=gym_id,
            member_id=member_id,
            subscription_id=subscription_id,
            amount=amount,
            method=method,
            type=type,
            reference_number=reference_number,
            notes=notes,
            status=PaymentStatus.COMPLETED,  # Assume completed for now
            created_by=created_by,
            updated_by=created_by
        )
        
        created = await self.payment_repo.create(payment)
        await self.session.commit()
        
        # Generate invoice for completed payment
        if created.status == PaymentStatus.COMPLETED:
            try:
                gym = await gym_repo.get_by_id(gym_id)
                invoice_service = InvoiceService(
                    invoice_repo=__import__('app.repositories.invoice_repo', fromlist=['InvoiceRepository']).InvoiceRepository(self.session),
                    session=self.session
                )
                await invoice_service.create_invoice_for_payment(
                    payment=created,
                    gym=gym,
                    member_name=member.name,
                    member_phone=member.phone
                )
                logger.info(f"Invoice generated for payment {created.id}")
            except Exception as e:
                logger.error(f"Failed to generate invoice for payment {created.id}: {str(e)}")
                # Don't fail payment creation if invoice generation fails
        
        logger.info(f"Payment {created.id} recorded for member {member_id} by staff {created_by}")
        return created

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
            raise NotFoundError(f"Payment {payment_id} not found in gym {gym_id}")
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
            raise NotFoundError(f"No invoice found for payment {payment_id}")
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