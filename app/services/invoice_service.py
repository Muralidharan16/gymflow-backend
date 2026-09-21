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
        sgst_percentage: Optional[Decimal] = None,
    ):
        """PAY-15: legacy invoice creation is permanently retired."""
        del (
            payment,
            gym,
            member_name,
            member_phone,
            gst_percentage,
            cgst_percentage,
            sgst_percentage,
        )
        raise RuntimeError(
            "PAY-15 legacy invoice writes are retired; use Finance Core"
        )

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

    async def void_invoice(
        self,
        invoice_id: UUID,
        gym_id: UUID,
        staff_id: UUID,
    ) -> None:
        """PAY-15: legacy invoice status mutation is permanently retired."""
        del invoice_id, gym_id, staff_id
        raise RuntimeError(
            "PAY-15 legacy invoice writes are retired; use Finance Core"
        )

    async def regenerate_pdf(
        self,
        invoice_id: UUID,
        gym_id: UUID,
    ) -> str:
        """PAY-15: legacy invoice PDF persistence is permanently retired."""
        del invoice_id, gym_id
        raise RuntimeError(
            "PAY-15 legacy invoice writes are retired; historical PDFs are read-only"
        )

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