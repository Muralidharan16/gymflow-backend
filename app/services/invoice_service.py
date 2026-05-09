import uuid
from datetime import date
from decimal import Decimal
from app.models.payment import Invoice, Payment
from app.models.subscription import MemberSubscription
from app.models.member import Member
from app.models.enums import InvoiceType, InvoiceStatus
from app.repositories.payment_repo import InvoiceRepository
from sqlalchemy import select

# We'll use the session directly for gym lookup, to avoid dependency on GymRepository.
# But to keep it clean, we can accept a session.
class InvoiceService:
    def __init__(self, invoice_repo: InvoiceRepository, session):
        self.invoice_repo = invoice_repo
        self.session = session

    async def generate_invoice(self, gym_id: uuid.UUID, payment: Payment,
                               subscription: MemberSubscription, member: Member) -> Invoice:
        # Fetch tax settings using raw select
        from app.models.gym import BranchTaxSettings
        q = select(BranchTaxSettings).where(BranchTaxSettings.gym_id == gym_id)
        result = await self.session.execute(q)
        tax_settings = result.scalar_one_or_none()

        if tax_settings and tax_settings.is_active:
            invoice_type = InvoiceType.tax_invoice
            tax_rate = tax_settings.gst_rate
        else:
            invoice_type = InvoiceType.bill_of_supply
            tax_rate = Decimal("0")

        subtotal = payment.amount - payment.discount_amount
        tax_amount = (subtotal * tax_rate / 100).quantize(Decimal("0.01"))
        total_amount = subtotal + tax_amount

        # Generate invoice number
        # Get last sequence for gym
        seq = await self._next_sequence(gym_id)
        from app.models.gym import Gym
        qgym = select(Gym).where(Gym.id == gym_id)
        gym = (await self.session.execute(qgym)).scalar_one()
        invoice_number = f"{gym.gymu_id}-{date.today().year}-{seq:05d}"

        invoice = Invoice(
            gym_id=gym_id,
            member_id=member.id,
            payment_id=payment.id,
            subscription_id=subscription.id,
            invoice_number=invoice_number,
            subtotal=subtotal,
            discount_amount=payment.discount_amount,
            tax_amount=tax_amount,
            tax_rate=tax_rate,
            total_amount=total_amount,
            invoice_type=invoice_type,
            status=InvoiceStatus.issued
        )
        return await self.invoice_repo.create(invoice)

    async def _next_sequence(self, gym_id: uuid.UUID) -> int:
        # Get current max seq for this gym from today's invoices
        today = date.today()
        q = select(Invoice).where(
            Invoice.gym_id == gym_id,
            Invoice.issued_at >= today
        ).order_by(Invoice.invoice_number.desc()).limit(1)
        result = await self.session.execute(q)
        last_inv = result.scalar_one_or_none()
        if last_inv:
            try:
                return int(last_inv.invoice_number.split("-")[-1]) + 1
            except (IndexError, ValueError):
                return 1
        return 1