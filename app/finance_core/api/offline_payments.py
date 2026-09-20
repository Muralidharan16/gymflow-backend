from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import Staff, require_org_admin
from app.finance_core.api.schemas import (
    FinanceOfflinePaymentApprovalResponse,
    FinanceOfflinePaymentPrepareRequest,
    FinanceOfflinePaymentPrepareResponse,
    FinanceOfflinePaymentRejectRequest,
    FinanceOfflinePaymentRejectResponse,
)
from app.finance_core.services.offline_payments import FinanceOfflinePaymentService


router=APIRouter(
    prefix="/api/v1/organizations/{org_id}/finance/offline-payments",
    tags=["Finance Offline Payments"],
)


def _enforce_org(org_id: uuid.UUID, staff: Staff) -> None:
    if staff.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code":"OFFLINE_PAYMENT_TENANT_FORBIDDEN","message":"Offline payment tenant is invalid."},
        )


def _require_idempotency(value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code":"OFFLINE_PAYMENT_IDEMPOTENCY_REQUIRED","message":"X-Idempotency-Key is required."},
        )
    return value


def _map_db_error(exc: DBAPIError) -> HTTPException:
    sqlstate=getattr(exc.orig,"sqlstate",None) or getattr(exc.orig,"pgcode",None)
    if sqlstate=="42501":
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code":"OFFLINE_PAYMENT_AUTHORITY_FORBIDDEN","message":"Offline payment authority is invalid."},
        )
    if sqlstate in {"23505","23514","40001"}:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code":"OFFLINE_PAYMENT_STATE_CONFLICT","message":"Offline payment state conflicts with current Finance authority."},
        )
    if sqlstate=="22023":
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code":"OFFLINE_PAYMENT_REQUEST_INVALID","message":"Offline payment request is invalid."},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code":"OFFLINE_PAYMENT_INTERNAL_ERROR","message":"Offline payment operation failed safely."},
    )


@router.post("",response_model=FinanceOfflinePaymentPrepareResponse,status_code=status.HTTP_201_CREATED)
async def prepare_offline_payment(
    org_id: uuid.UUID,
    request: FinanceOfflinePaymentPrepareRequest,
    x_idempotency_key: str | None=Header(default=None,alias="X-Idempotency-Key"),
    db: AsyncSession=Depends(get_db),
    staff: Staff=Depends(require_org_admin),
) -> FinanceOfflinePaymentPrepareResponse:
    _enforce_org(org_id,staff)
    service=FinanceOfflinePaymentService(db)
    try:
        result=await service.prepare(
            invoice_id=request.invoice_id,
            payment_method=request.payment_method,
            amount=request.amount,
            currency_code=request.currency_code,
            reference_code=request.reference_code,
            proof_sha256=request.proof_sha256,
            idempotency_key=_require_idempotency(x_idempotency_key),
        )
    except DBAPIError as exc:
        raise _map_db_error(exc) from exc
    return FinanceOfflinePaymentPrepareResponse(**result.__dict__)


@router.post(
    "/{offline_payment_request_id}/approve",
    response_model=FinanceOfflinePaymentApprovalResponse,
)
async def approve_offline_payment(
    org_id: uuid.UUID,
    offline_payment_request_id: uuid.UUID,
    x_idempotency_key: str | None=Header(default=None,alias="X-Idempotency-Key"),
    db: AsyncSession=Depends(get_db),
    staff: Staff=Depends(require_org_admin),
) -> FinanceOfflinePaymentApprovalResponse:
    _enforce_org(org_id,staff)
    service=FinanceOfflinePaymentService(db)
    try:
        result=await service.approve(
            offline_payment_request_id=offline_payment_request_id,
            idempotency_key=_require_idempotency(x_idempotency_key),
        )
    except DBAPIError as exc:
        raise _map_db_error(exc) from exc
    return FinanceOfflinePaymentApprovalResponse(**result.__dict__)


@router.post(
    "/{offline_payment_request_id}/reject",
    response_model=FinanceOfflinePaymentRejectResponse,
)
async def reject_offline_payment(
    org_id: uuid.UUID,
    offline_payment_request_id: uuid.UUID,
    request: FinanceOfflinePaymentRejectRequest,
    x_idempotency_key: str | None=Header(default=None,alias="X-Idempotency-Key"),
    db: AsyncSession=Depends(get_db),
    staff: Staff=Depends(require_org_admin),
) -> FinanceOfflinePaymentRejectResponse:
    _enforce_org(org_id,staff)
    service=FinanceOfflinePaymentService(db)
    try:
        result=await service.reject(
            offline_payment_request_id=offline_payment_request_id,
            reason_code=request.reason_code,
            idempotency_key=_require_idempotency(x_idempotency_key),
        )
    except DBAPIError as exc:
        raise _map_db_error(exc) from exc
    return FinanceOfflinePaymentRejectResponse(**result.__dict__)
