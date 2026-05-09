import uuid
from datetime import date
from decimal import Decimal
from app.models.payment import Invoice, Payment
from app.models.subscription import MemberSubscription
from app.models.member import Member
from app.models.enums import InvoiceType, InvoiceStatus
from app.repositories.payment_repo import InvoiceRepository
from app.repositories.gym_repo import GymRepository, TaxRepository

class InvoiceService:
    def __init__(self, invoice_repo: InvoiceRepository, tax_repo: TaxRepository, gym_repo: GymRepository):
        self.invoice_repo = invoice_repo
        self.tax_repo = tax_repo
        self.gym_repo = gym_repo

    async def generate_invoice(self, gym_id: uuid.UUID, payment: Payment, subscription: MemberSubscription, member: Member) -> Invoice:
        # Rule 7: Invoice Generation
        tax_settings = await self.tax_repo.get_by_gym_id(gym_id)

        if tax_settings and tax_settings.is_active:
            invoice_type = InvoiceType.tax_invoice
            tax_rate = tax_settings.gst_rate
        else:
            invoice_type = InvoiceType.bill_of_supply
            tax_rate = Decimal("0")

        subtotal = payment.amount - payment.discount_amount
        tax_amount = (subtotal * tax_rate / 100).quantize(Decimal("0.01"))
        total_amount = subtotal + tax_amount

        seq = await self.invoice_repo.next_sequence_for_gym(gym_id)
        gym = await self.gym_repo.get_by_id(gym_id, gym_id) # gym_id twice since get_by_id expects id and gym_id
        invoice_number = f"{gym.gymu_id}-{date.today().year}-{seq:05d}"

        return await self.invoice_repo.create(Invoice(
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
        ))
