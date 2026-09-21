from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import Staff, require_gym_access
from app.core.exceptions import NotFoundError
from app.models.enums import PaymentMethod, PaymentStatus, PaymentType
from app.schemas.common import PaginatedResponse, Response
from app.schemas.payment import InvoiceResponse, PaymentCreate, PaymentResponse
from app.services.payment_service import PaymentService


router = APIRouter(prefix="/gyms/{gym_id}/payments", tags=["Payments"])


def _legacy_financial_write_retired() -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "message": (
                "Legacy member payment mutation is retired. "
                "Use the Finance Core payment workflow."
            ),
            "error_code": "LEGACY_PAYMENT_WRITE_RETIRED",
        },
    )


@router.post("", response_model=Response[PaymentResponse])
async def record_payment(
    gym_id: UUID,
    data: PaymentCreate,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db),
):
    """PAY-15: legacy payment creation is permanently disabled."""
    del gym_id, data, current_staff, db
    _legacy_financial_write_retired()


@router.get("", response_model=PaginatedResponse[PaymentResponse])
async def list_payments(
    gym_id: UUID,
    date_from: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    method: Optional[PaymentMethod] = Query(None, description="Payment method"),
    type: Optional[PaymentType] = Query(None, description="Payment type"),
    status_filter: Optional[PaymentStatus] = Query(
        None,
        alias="status",
        description="Payment status",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db),
):
    """Historical read-only legacy payment compatibility endpoint."""
    del current_staff
    service = PaymentService(db)
    payments, total = await service.list_payments(
        gym_id=gym_id,
        date_from=date_from,
        date_to=date_to,
        method=method,
        type=type,
        status=status_filter,
        page=page,
        size=size,
    )
    return PaginatedResponse(
        data=[PaymentResponse.model_validate(payment) for payment in payments],
        page=page,
        size=size,
        total=total,
    )


@router.get("/{payment_id}", response_model=Response[PaymentResponse])
async def get_payment_detail(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db),
):
    """Historical read-only legacy payment detail."""
    del current_staff
    service = PaymentService(db)
    try:
        payment = await service.get_payment(payment_id, gym_id)
        return Response(data=PaymentResponse.model_validate(payment))
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(exc), "error_code": "NOT_FOUND"},
        ) from exc


@router.get("/{payment_id}/invoice", response_model=Response[InvoiceResponse])
async def get_payment_invoice(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db),
):
    """
    Historical read-only invoice detail.

    PAY-15 deliberately does not lazily regenerate or persist a legacy PDF.
    """
    del current_staff
    service = PaymentService(db)
    try:
        invoice = await service.get_invoice_for_payment(payment_id, gym_id)
        return Response(data=InvoiceResponse.model_validate(invoice))
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": str(exc), "error_code": "NOT_FOUND"},
        ) from exc


@router.post("/{payment_id}/invoice/void")
async def void_invoice(
    gym_id: UUID,
    payment_id: UUID,
    current_staff: Staff = Depends(require_gym_access),
    db: AsyncSession = Depends(get_db),
):
    """PAY-15: legacy invoice monetary mutation is permanently disabled."""
    del gym_id, payment_id, current_staff, db
    _legacy_financial_write_retired()
