import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.payment import Invoice, InvoiceStatus
from app.models.gym import Gym, BranchTaxSettings
from app.repositories.payment_repo import InvoiceRepository
from app.utils.pdf import generate_invoice_pdf
from app.core.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)


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
        payment,
        gym: Gym,
        member_name: str,
        member_phone: str,
        gst_percentage: Optional[Decimal] = None,
        cgst_percentage: Optional[Decimal] = None,
        sgst_percentage: Optional[Decimal] = None
    ):
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
        from app.models.payment import PaymentStatus

        if payment.status != PaymentStatus.COMPLETED:
            raise ValueError("Cannot create invoice for non-completed payment")

        # Check if invoice already exists
        existing = await self.invoice_repo.get_by_payment(payment.id)
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

        # Fetch the organization's primary address to snapshot
        from app.models.address import OrganizationAddress
        addr_stmt = (
            select(OrganizationAddress)
            .where(OrganizationAddress.org_id == gym.org_id)
            .where(OrganizationAddress.is_primary == True)
            .where(OrganizationAddress.deleted_at.is_(None))
        )
        addr_res = await self.session.execute(addr_stmt)
        primary_addr = addr_res.scalar_one_or_none()
        
        snapshot_dict = None
        if primary_addr:
            from app.services.address_service import capture_address_snapshot
            try:
                snapshot_dict = await capture_address_snapshot(primary_addr.id, self.session)
            except Exception as e:
                logger.error(f"Failed to capture address snapshot for organization {gym.org_id}: {str(e)}")

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
            created_by=payment.created_by,
            billing_address_snapshot=snapshot_dict
        )

        created = await self.invoice_repo.create(invoice)
        await self.session.commit()

        # Generate PDF in background? For now generate synchronously
        try:
            # Build line items from payment (assuming one line item per payment)
            line_items = [{"description": f"Subscription payment", "amount": subtotal}]
            pdf_bytes = generate_invoice_pdf(
                invoice_number=invoice_number,
                gym_name=gym.name,
                member_name=member_name,
                member_phone=member_phone,
                line_items=line_items,
                subtotal=subtotal,
                tax_rate=gst_percentage,
                tax_amount=total_tax,
                total_amount=total_amount,
                invoice_type="tax_invoice" if gst_percentage > 0 else "bill_of_supply",
                issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                gst_number=getattr(gym, "gst_number", None),
                sac_code="996319"
            )
            # Store PDF bytes? Original design: pdf_url as string. We'll keep pdf_url as placeholder.
            # In real implementation, you would upload to S3/minio and set URL.
            # For now, we store a placeholder indicating PDF is available.
            created.pdf_url = f"invoice_{invoice_number}.pdf"
            await self.invoice_repo.update(created)
            await self.session.commit()
            logger.info(f"Generated PDF for invoice {created.id}")
        except Exception as e:
            logger.error(f"Failed to generate PDF for invoice {created.id}: {str(e)}")
            # Don't fail invoice creation if PDF generation fails

        return created

    async def get_invoice_by_payment(self, payment_id: UUID):
        """Get invoice by payment ID."""
        return await self.invoice_repo.get_by_payment(payment_id)

    async def get_invoice(self, invoice_id: UUID, gym_id: Optional[UUID] = None):
        """Get invoice by ID with optional gym scoping."""
        invoice = await self.invoice_repo.get_by_id_and_gym(invoice_id, gym_id) if gym_id else None
        if not invoice:
            # Fallback if no gym_id: try to get without gym scope (less secure)
            if not gym_id:
                from sqlalchemy import select
                from app.models.payment import Invoice
                query = select(Invoice).where(Invoice.id == invoice_id)
                result = await self.session.execute(query)
                invoice = result.scalar_one_or_none()
            if not invoice:
                raise NotFoundError(f"Invoice {invoice_id} not found", error_code="NOT_FOUND")
        return invoice

    async def void_invoice(self, invoice_id: UUID, gym_id: UUID, staff_id: UUID) -> None:
        """Void an invoice (only allowed if payment is still refundable)."""
        invoice = await self.get_invoice(invoice_id, gym_id)

        if invoice.status != InvoiceStatus.PAID:
            raise ValidationError(f"Cannot void invoice with status {invoice.status}", error_code="VALIDATION_ERROR")

        # Check if payment is refundable (e.g., not too old, etc.)
        payment = invoice.payment
        from app.models.payment import PaymentStatus
        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError("Payment not in completed state", error_code="VALIDATION_ERROR")

        # Mark invoice as void
        invoice.status = InvoiceStatus.VOID
        invoice.updated_by = staff_id
        invoice.updated_at = datetime.now(timezone.utc)

        await self.invoice_repo.update(invoice)
        await self.session.commit()

        logger.info(f"Voided invoice {invoice_id} by staff {staff_id}")

    async def regenerate_pdf(self, invoice_id: UUID, gym_id: UUID) -> str:
        """Regenerate PDF for an invoice."""
        invoice = await self.get_invoice(invoice_id, gym_id)

        # Build line items
        line_items = [{"description": f"Payment {invoice.payment_id}", "amount": invoice.subtotal}]
        pdf_bytes = generate_invoice_pdf(
            invoice_number=invoice.invoice_number,
            gym_name=invoice.gym.name,
            member_name=invoice.member_name,
            member_phone=invoice.member_phone,
            line_items=line_items,
            subtotal=invoice.subtotal,
            tax_rate=invoice.gst_percentage,
            tax_amount=invoice.total_tax,
            total_amount=invoice.total_amount,
            invoice_type="tax_invoice" if invoice.gst_percentage > 0 else "bill_of_supply",
            issued_at=invoice.payment_date.strftime("%Y-%m-%d %H:%M:%S") if invoice.payment_date else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            gst_number=getattr(invoice.gym, "gst_number", None),
            sac_code="996319"
        )
        # In production, upload to storage and set URL
        invoice.pdf_url = f"invoice_{invoice.invoice_number}.pdf"
        await self.invoice_repo.update(invoice)
        await self.session.commit()
        return invoice.pdf_url

    async def _get_active_tax_config(self, gym_id: UUID):
        """Get active tax configuration for a gym."""
        query = select(BranchTaxSettings).where(
            BranchTaxSettings.gym_id == gym_id,
            BranchTaxSettings.is_active == True
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def _generate_invoice_number(self, gym_id: UUID) -> str:
        """Generate unique invoice number: INV-{gym_code}-{YYYYMMDD}-{sequence}"""
        today = datetime.now(timezone.utc).date()
        date_str = today.strftime("%Y%m%d")
        seq = await self.invoice_repo.next_sequence_for_gym(gym_id)

        # Get gym short code
        gym = await self.session.get(Gym, gym_id)
        gym_code = gym.name[:3].upper() if gym else "GYM"

        return f"INV-{gym_code}-{date_str}-{seq:04d}"