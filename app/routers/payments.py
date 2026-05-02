from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..middleware.auth_middleware import get_current_owner
from ..schemas.payment import PaymentCreate, PaymentRead
from ..models.models import Payment, Member

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get('/', response_model=list[PaymentRead])
async def list_payments(owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Payment).where(Payment.gym_id == owner.gym_id).order_by(Payment.payment_date.desc()).limit(200))
    rows = q.scalars().all()
    return rows


@router.post('/', status_code=status.HTTP_201_CREATED)
async def create_payment(payload: PaymentCreate, owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    member = None
    if payload.member_id:
        q = await db.execute(select(Member).where(Member.id == payload.member_id, Member.gym_id == owner.gym_id))
        member = q.scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')
    p = Payment(gym_id=owner.gym_id, member_id=payload.member_id, amount=payload.amount, razorpay_id=payload.razorpay_id, status='success')
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {"id": str(p.id)}
