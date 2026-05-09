from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_active_staff, require_gym_access
from app.models.staff import Staff
from app.models.payment import PaymentMethod, PaymentType, PaymentStatus
from app.schemas.common import Response, PaginatedResponse, MessageResponse
from app.schemas.payment import PaymentResponse, PaymentCreate, InvoiceResponse
from app.services.payment_service import PaymentService
from app.services.invoice_service import InvoiceService
from app.repositories.payment_repo import InvoiceRepository  # FIXED: was invoice_repo
from app.core.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/gyms/{gym_id}/payments", tags=["Payments"])


@router.post("", response_model=Response[PaymentResponse])
async def record_payment(
    gym_id: UUID,
    data: PaymentCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Record a payment and automatically generate an invoice if status is completed.
    """
    service = PaymentService(db)
    try:
        payment = await service.create_payment(
            gym_id=gym_id,
            member_id=data.member_id,
            subscription_id=data.subscription_id,
            amount=data.amount,
            method=data.method,
            type=data.type,
            reference_number=data.reference_number,
            notes=data.notes,
            created_by=current_staff.id
        )
        await db.commit()
        return Response(data=PaymentResponse.model_validate(payment))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": e.error_code}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )


@router.get("", response_model=PaginatedResponse[PaymentResponse])
async def list_payments(
    gym_id: UUID,
    date_from: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    method: Optional[PaymentMethod] = Query(None, description="Payment method"),
    type: Optional[PaymentType] = Query(None, description="Payment type"),
    status: Optional[PaymentStatus] = Query(None, description="Payment status"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    List payments with pagination and filters.
    """
    service = PaymentService(db)
    payments, total = await service.list_payments(
        gym_id=gym_id,
        date_from=date_from,
        date_to=date_to,
        method=method,
        type=type,
        status=status,
        page=page,
        size=size
    )
    return PaginatedResponse(
        data=[PaymentResponse.model_validate(p) for p in payments],
        page=page,
        size=size,
        total=total
    )


@router.get("/{payment_id}", response_model=Response[PaymentResponse])
async def get_payment_detail(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get single payment detail.
    """
    service = PaymentService(db)
    try:
        payment = await service.get_payment(payment_id, gym_id)
        return Response(data=PaymentResponse.model_validate(payment))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )


@router.get("/{payment_id}/invoice", response_model=Response[InvoiceResponse])
async def get_payment_invoice(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Get invoice detail for a payment. Generate PDF URL if not already generated.
    """
    service = PaymentService(db)
    try:
        invoice = await service.get_invoice_for_payment(payment_id, gym_id)
        # If pdf_url is null, try to regenerate
        if not invoice.pdf_url:
            inv_service = InvoiceService(InvoiceRepository(db), db)
            pdf_url = await inv_service.regenerate_pdf(invoice.id, gym_id)
            invoice.pdf_url = pdf_url
            await db.commit()
        return Response(data=InvoiceResponse.model_validate(invoice))
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": f"Failed to generate invoice PDF: {str(e)}", "error_code": "INTERNAL_ERROR"}
        )


@router.post("/{payment_id}/invoice/void", response_model=Response[MessageResponse])
async def void_invoice(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db)
):
    """
    Void the invoice associated with a payment (sets invoice status to void).
    Only allowed if payment is still refundable.
    """
    service = PaymentService(db)
    inv_service = InvoiceService(InvoiceRepository(db), db)
    try:
        invoice = await service.get_invoice_for_payment(payment_id, gym_id)
        await inv_service.void_invoice(invoice.id, gym_id, current_staff.id)
        await db.commit()
        return Response(data=MessageResponse(message="Invoice voided successfully"))
    except NotFoundError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(e), "error_code": "NOT_FOUND"}
        )
    except ValidationError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(e), "error_code": e.error_code}
        )