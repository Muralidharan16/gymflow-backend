from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..schemas.payment import PaymentCreateRequest, PaymentResponse, PaymentRead
from ..models.models import Payment, StaffRole
from ..services.subscriptions import process_idempotent_payment

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get('/', response_model=list[PaymentRead])
async def list_payments(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    """List payments for the current org, most recent first."""
    stmt = (
        select(Payment)
        .where(Payment.org_id == context.org_id)
        .order_by(Payment.payment_date.desc())
        .limit(200)
    )
    q = await db.execute(stmt)
    rows = q.scalars().all()
    return rows


@router.post('/', response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreateRequest,
    x_idempotency_key: str = Header(..., description="Client-generated idempotency key"),
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin, StaffRole.receptionist])),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a payment and provision a subscription atomically.
    
    Requires X-Idempotency-Key header to prevent double-charges.
    The branch_id is derived from the staff's primary branch.
    """
    if not context.primary_branch_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Staff has no assigned branch")

    try:
        result = await process_idempotent_payment(
            db=db,
            idempotency_key=x_idempotency_key,
            org_id=str(context.org_id),
            branch_id=str(context.primary_branch_id),
            member_id=payload.member_id,
            plan_id=payload.plan_id,
            amount_paid=payload.amount,
            payment_method=payload.payment_method,
            payment_source=payload.payment_source,
            renewal_type=payload.renewal_type,
            staff_id=str(context.staff_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return PaymentResponse(**result)
