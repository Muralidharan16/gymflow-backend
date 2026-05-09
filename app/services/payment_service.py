import uuid
from app.models.payment import Payment
from app.models.enums import PaymentStatus
from app.repositories.payment_repo import PaymentRepository
from app.schemas.payment import PaymentCreate

class PaymentService:
    def __init__(self, payment_repo: PaymentRepository):
        self.payment_repo = payment_repo

    async def record_payment(self, gym_id: uuid.UUID, data: PaymentCreate, staff_id: uuid.UUID) -> Payment:
        payment = Payment(
            gym_id=gym_id,
            member_id=data.member_id,
            subscription_id=data.subscription_id,
            collected_by=staff_id,
            amount=data.amount,
            discount_amount=data.discount_amount,
            payment_method=data.payment_method,
            payment_type=data.payment_type,
            status=PaymentStatus.completed,
            notes=data.notes
        )
        return await self.payment_repo.create(payment)
