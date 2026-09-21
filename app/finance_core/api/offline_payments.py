from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
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
from app.finance_core.models.foundation import FinanceOfflinePaymentRequest
from app.finance_core.security_abuse import (
    FinanceSecurityContext,
    enforce_maker_checker,
    finance_high_risk_actor_dependency,
    security_event,
    validate_admin_reason_code,
)
from app.finance_core.services.offline_payments import FinanceOfflinePaymentService
from app.finance_core.services.security_audit import FinanceSecurityAuditService


logger = logging.getLogger("doers.finance.offline_payments")
PAY16_APPROVAL_VELOCITY_WINDOW_SECONDS = 5 * 60
PAY16_APPROVAL_VELOCITY_LIMIT = 10


router=APIRouter(
    prefix="/api/v1/organizations/{org_id}/finance/offline-payments",
    tags=["Finance Offline Payments"],
)


def _enforce_org(org_id: uuid.UUID, staff: Staff) -> None:
    if staff.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code":"OFFLINE_PAYMENT_TENANT_NOT_FOUND","message":"Offline payment resource was not found."},
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


async def _approval_security_preflight(
    *,
    db: AsyncSession,
    org_id: uuid.UUID,
    offline_payment_request_id: uuid.UUID,
    context: FinanceSecurityContext,
) -> None:
    maker_actor_id = await db.scalar(
        select(FinanceOfflinePaymentRequest.prepared_actor_id).where(
            FinanceOfflinePaymentRequest.id == offline_payment_request_id,
            FinanceOfflinePaymentRequest.organization_id == org_id,
        )
    )
    if maker_actor_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "OFFLINE_PAYMENT_NOT_FOUND",
                "message": "Offline payment resource was not found.",
            },
        )

    try:
        enforce_maker_checker(
            maker_actor_id=maker_actor_id,
            checker_actor_id=context.actor_id,
        )
    except HTTPException:
        security_event(
            "finance.offline_payment.maker_checker_rejected",
            actor_id=context.actor_id,
            organization_id=org_id,
            severity="warning",
        )
        raise

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=PAY16_APPROVAL_VELOCITY_WINDOW_SECONDS
    )
    approvals = await db.scalar(
        select(func.count())
        .select_from(FinanceOfflinePaymentRequest)
        .where(
            FinanceOfflinePaymentRequest.organization_id == org_id,
            FinanceOfflinePaymentRequest.approved_actor_id == context.actor_id,
            FinanceOfflinePaymentRequest.status == "approved",
            FinanceOfflinePaymentRequest.decided_at >= cutoff,
        )
    )
    if int(approvals or 0) >= PAY16_APPROVAL_VELOCITY_LIMIT:
        security_event(
            "finance.offline_payment.velocity_rejected",
            actor_id=context.actor_id,
            organization_id=org_id,
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "OFFLINE_PAYMENT_APPROVAL_VELOCITY_EXCEEDED",
                "message": "Offline payment approval velocity limit was exceeded.",
            },
        )


@router.post("",response_model=FinanceOfflinePaymentPrepareResponse,status_code=status.HTTP_201_CREATED)
async def prepare_offline_payment(
    org_id: uuid.UUID,
    request: FinanceOfflinePaymentPrepareRequest,
    x_idempotency_key: str | None=Header(default=None,alias="X-Idempotency-Key"),
    db: AsyncSession=Depends(get_db),
    staff: Staff=Depends(require_org_admin),
    security_context: FinanceSecurityContext=Depends(finance_high_risk_actor_dependency),
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

    await FinanceSecurityAuditService(db).record(
        event_type="finance.security.offline_payment.prepared",
        target_type="offline_payment",
        target_id=result.offline_payment_request_id,
        reason_code="OFFLINE_PAYMENT_PREPARED",
        severity="info",
    )
    security_event(
        "finance.offline_payment.prepared",
        actor_id=security_context.actor_id,
        organization_id=org_id,
        reason_code="OFFLINE_PAYMENT_PREPARED",
    )
    return FinanceOfflinePaymentPrepareResponse(**result.__dict__)


@router.post(
    "/{offline_payment_request_id}/approve",
    response_model=FinanceOfflinePaymentApprovalResponse,
)
async def approve_offline_payment(
    org_id: uuid.UUID,
    offline_payment_request_id: uuid.UUID,
    x_idempotency_key: str | None=Header(default=None,alias="X-Idempotency-Key"),
    x_finance_reason_code: str | None=Header(default=None,alias="X-Finance-Reason-Code"),
    db: AsyncSession=Depends(get_db),
    staff: Staff=Depends(require_org_admin),
    security_context: FinanceSecurityContext=Depends(finance_high_risk_actor_dependency),
) -> FinanceOfflinePaymentApprovalResponse:
    _enforce_org(org_id,staff)
    reason_code=validate_admin_reason_code(x_finance_reason_code)
    await _approval_security_preflight(
        db=db,
        org_id=org_id,
        offline_payment_request_id=offline_payment_request_id,
        context=security_context,
    )
    service=FinanceOfflinePaymentService(db)
    try:
        result=await service.approve(
            offline_payment_request_id=offline_payment_request_id,
            idempotency_key=_require_idempotency(x_idempotency_key),
        )
    except DBAPIError as exc:
        raise _map_db_error(exc) from exc
    await FinanceSecurityAuditService(db).record(
        event_type="finance.security.offline_payment.approved",
        target_type="offline_payment",
        target_id=offline_payment_request_id,
        reason_code=reason_code,
        severity="info",
    )
    security_event(
        "finance.offline_payment.approved",
        actor_id=security_context.actor_id,
        organization_id=org_id,
        reason_code=reason_code,
    )
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
    security_context: FinanceSecurityContext=Depends(finance_high_risk_actor_dependency),
) -> FinanceOfflinePaymentRejectResponse:
    _enforce_org(org_id,staff)
    reason_code=validate_admin_reason_code(request.reason_code)
    service=FinanceOfflinePaymentService(db)
    try:
        result=await service.reject(
            offline_payment_request_id=offline_payment_request_id,
            reason_code=reason_code,
            idempotency_key=_require_idempotency(x_idempotency_key),
        )
    except DBAPIError as exc:
        raise _map_db_error(exc) from exc
    await FinanceSecurityAuditService(db).record(
        event_type="finance.security.offline_payment.rejected",
        target_type="offline_payment",
        target_id=offline_payment_request_id,
        reason_code=reason_code,
        severity="info",
    )
    security_event(
        "finance.offline_payment.rejected",
        actor_id=security_context.actor_id,
        organization_id=org_id,
        reason_code=reason_code,
    )
    return FinanceOfflinePaymentRejectResponse(**result.__dict__)
