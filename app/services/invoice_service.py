from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentStatus, PaymentType
from app.models.gym import Gym
from app.repositories.invoice_repo import InvoiceRepository
from app.utils.pdf import generate_invoice_pdf
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import logger


class InvoiceService:
    """
    Service for managing invoices linked to payments.
    """
    
    def __init__(self, invoice_repo: InvoiceRepository, session: AsyncSession):
        """
        Initialize InvoiceService.
        
        Args:
            invoice_repo: Repository for invoice operations
            session: Database session (provides access to other repositories if needed)
        """
        self.invoice_repo = invoice_repo
        self.session = session
    
    async def create_invoice_for_payment(
        self,
        payment: Payment,
        gym: Gym,
        member_name: str,
        member_phone: str,
        gst_percentage: Optional[Decimal] = None,
        cgst_percentage: Optional[Decimal] = None,
        sgst_percentage: Optional[Decimal] = None
    ) -> Invoice:
        """
        Create invoice for a completed payment.
        
        Args:
            payment: Payment object (must be completed)
            gym: Gym object with tax settings
            member_name: Member's full name
            member_phone: Member's phone number
            gst_percentage: GST percentage (overrides gym tax config if provided)
            cgst_percentage: CGST percentage
            sgst_percentage: SGST percentage
        
        Returns:
            Created invoice
        
        Raises:
            ValueError: If payment is not completed
            ValidationError: If tax calculation fails
        """
        if payment.status != PaymentStatus.COMPLETED:
            raise ValueError("Cannot create invoice for non-completed payment")
        
        # Check if invoice already exists
        existing = await self.invoice_repo.get_by_payment_id(payment.id)
        if existing:
            logger.warning(f"Invoice already exists for payment {payment.id}")
            return existing
        
        # Calculate tax components
        if gst_percentage is None:
            # Use gym's tax config
            tax_config = await self._get_active_tax_config(gym.id)
            if tax_config:
                gst_percentage = tax_config.gst_percentage
                cgst_percentage = tax_config.cgst_percentage
                sgst_percentage = tax_config.sgst_percentage
            else:
                gst_percentage = Decimal('0')
                cgst_percentage = Decimal('0')
                sgst_percentage = Decimal('0')
        
        # Calculate tax amounts
        subtotal = payment.amount
        gst_amount = (subtotal * gst_percentage) / Decimal('100')
        cgst_amount = (subtotal * cgst_percentage) / Decimal('100') if cgst_percentage else Decimal('0')
        sgst_amount = (subtotal * sgst_percentage) / Decimal('100') if sgst_percentage else Decimal('0')
        total_tax = gst_amount + cgst_amount + sgst_amount
        total_amount = subtotal + total_tax
        
        # Generate invoice number
        invoice_number = await self._generate_invoice_number(gym.id)
        
        # Create invoice
        invoice = Invoice(
            invoice_number=invoice_number,
            payment_id=payment.id,
            gym_id=gym.id,
            member_name=member_name,
            member_phone=member_phone,
            subtotal=subtotal,
            gst_percentage=gst_percentage,
            cgst_percentage=cgst_percentage or Decimal('0'),
            sgst_percentage=sgst_percentage or Decimal('0'),
            gst_amount=gst_amount,
            cgst_amount=cgst_amount,
            sgst_amount=sgst_amount,
            total_tax=total_tax,
            total_amount=total_amount,
            status=InvoiceStatus.PAID,
            payment_date=payment.payment_date or datetime.now(timezone.utc),
            created_by=payment.created_by
        )
        
        created = await self.invoice_repo.create(invoice)
        await self.session.commit()
        
        # Generate PDF in background? For now generate synchronously
        try:
            pdf_url = await generate_invoice_pdf(created)
            created.pdf_url = pdf_url
            await self.invoice_repo.update(created)
            await self.session.commit()
            logger.info(f"Generated PDF for invoice {created.id}")
        except Exception as e:
            logger.error(f"Failed to generate PDF for invoice {created.id}: {str(e)}")
            # Don't fail invoice creation if PDF generation fails
        
        return created
    
    async def get_invoice_by_payment(self, payment_id: UUID) -> Optional[Invoice]:
        """
        Get invoice by payment ID.
        
        Args:
            payment_id: UUID of payment
        
        Returns:
            Invoice if found, else None
        """
        return await self.invoice_repo.get_by_payment_id(payment_id)
    
    async def get_invoice(self, invoice_id: UUID, gym_id: Optional[UUID] = None) -> Invoice:
        """
        Get invoice by ID with optional gym scoping.
        
        Args:
            invoice_id: UUID of invoice
            gym_id: Optional gym ID for access control
        
        Returns:
            Invoice object
        
        Raises:
            NotFoundError: If invoice not found or gym mismatch
        """
        invoice = await self.invoice_repo.get_by_id(invoice_id)
        if not invoice:
            raise NotFoundError(f"Invoice {invoice_id} not found")
        
        if gym_id and invoice.gym_id != gym_id:
            raise NotFoundError(f"Invoice {invoice_id} not found in this gym")
        
        return invoice
    
    async def void_invoice(self, invoice_id: UUID, gym_id: UUID, staff_id: UUID) -> None:
        """
        Void an invoice (only allowed if payment is still refundable).
        
        Args:
            invoice_id: UUID of invoice
            gym_id: Gym ID for access control
            staff_id: Staff member performing void
        
        Raises:
            NotFoundError: If invoice not found
            ValidationError: If invoice cannot be voided
        """
        invoice = await self.get_invoice(invoice_id, gym_id)
        
        if invoice.status != InvoiceStatus.PAID:
            raise ValidationError(f"Cannot void invoice with status {invoice.status}")
        
        # Check if payment is refundable (e.g., not too old, etc.)
        payment = invoice.payment
        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError("Payment not in completed state")
        
        # Mark invoice as void
        invoice.status = InvoiceStatus.VOID
        invoice.updated_by = staff_id
        invoice.updated_at = datetime.now(timezone.utc)
        
        await self.invoice_repo.update(invoice)
        await self.session.commit()
        
        logger.info(f"Voided invoice {invoice_id} by staff {staff_id}")
    
    async def regenerate_pdf(self, invoice_id: UUID, gym_id: UUID) -> str:
        """
        Regenerate PDF for an invoice.
        
        Args:
            invoice_id: UUID of invoice
            gym_id: Gym ID for access control
        
        Returns:
            URL of generated PDF
        """
        invoice = await self.get_invoice(invoice_id, gym_id)
        
        pdf_url = await generate_invoice_pdf(invoice)
        invoice.pdf_url = pdf_url
        invoice.updated_at = datetime.now(timezone.utc)
        
        await self.invoice_repo.update(invoice)
        await self.session.commit()
        
        return pdf_url
    
    async def _get_active_tax_config(self, gym_id: UUID):
        """Get active tax configuration for a gym."""
        from app.models.gym import BranchTaxSettings
        from sqlalchemy import select
        
        query = select(BranchTaxSettings).where(
            BranchTaxSettings.gym_id == gym_id,
            BranchTaxSettings.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def _generate_invoice_number(self, gym_id: UUID) -> str:
        """
        Generate unique invoice number: INV-{gym_prefix}-{YYYYMMDD}-{sequence}
        """
        # Get last invoice for today
        today = datetime.now(timezone.utc).date()
        date_str = today.strftime("%Y%m%d")
        
        # Count invoices created today for this gym
        from sqlalchemy import func
        from app.models.invoice import Invoice
        
        query = select(func.count(Invoice.id)).where(
            Invoice.gym_id == gym_id,
            func.date(Invoice.created_at) == today
        )
        result = await self.session.execute(query)
        count = result.scalar() or 0
        sequence = str(count + 1).zfill(4)
        
        # Get gym short code (first 3 chars of gym name, uppercase)
        gym = await self.session.get(Gym, gym_id)
        if not gym:
            gym_code = "GYM"
        else:
            gym_code = gym.name[:3].upper()
        
        return f"INV-{gym_code}-{date_str}-{sequence}"