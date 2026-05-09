import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.payment import PaymentCreate, PaymentResponse, InvoiceResponse
from app.schemas.common import Response
from app.services.payment_service import PaymentService
from app.services.invoice_service import InvoiceService
from app.repositories.payment_repo import PaymentRepository, InvoiceRepository
from app.repositories.gym_repo import GymRepository, TaxRepository
from app.repositories.member_repo import MemberRepository
from app.repositories.subscription_repo import SubscriptionRepository

router = APIRouter(prefix="/gyms/{gym_id}/payments", tags=["Payments"])

@router.post("", response_model=Response[PaymentResponse])
async def record_payment(gym_id: uuid.UUID, data: PaymentCreate, request: Request, db: AsyncSession = Depends(get_db)):
    service = PaymentService(PaymentRepository(db))
    payment = await service.record_payment(gym_id, data, request.state.staff_id)
    
    # Auto-generate invoice
    inv_service = InvoiceService(InvoiceRepository(db), TaxRepository(db), GymRepository(db))
    sub_repo = SubscriptionRepository(db)
    member_repo = MemberRepository(db)
    
    sub = await sub_repo.get_by_id(data.subscription_id, gym_id) if data.subscription_id else None
    member = await member_repo.get_by_id(data.member_id, gym_id)
    
    if sub and member:
        await inv_service.generate_invoice(gym_id, payment, sub, member)
        
    return Response(data=payment, message="Payment recorded and invoice generated")
