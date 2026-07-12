from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.finance_core.api.auth import (
    checkout_actor_dependency,
    checkout_status_actor_dependency,
    finance_admin_actor_dependency,
    internal_payment_application_actor_dependency,
    webhook_actor_dependency,
)
from app.finance_core.api.guards import require_finance_payment_api_enabled
from app.finance_core.api.schemas import (
    FinanceAdminPaymentStatusResponse,
    FinanceCheckoutCreateRequest,
    FinanceCheckoutCreateResponse,
    FinanceCheckoutStatusResponse,
    FinanceInternalPaymentApplicationRequest,
    FinanceInternalPaymentApplicationResponse,
)


router = APIRouter(
    prefix="/api/v1/finance/payments",
    tags=["Finance Payment Boundary"],
)


@router.post("/checkout-sessions", response_model=FinanceCheckoutCreateResponse)
async def create_checkout_session(
    _request: FinanceCheckoutCreateRequest,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(checkout_actor_dependency),
) -> FinanceCheckoutCreateResponse:
    raise AssertionError("Finance payment API guard must reject before checkout creation.")


@router.get("/checkout-sessions/{checkout_session_id}", response_model=FinanceCheckoutStatusResponse)
async def get_checkout_session_status(
    checkout_session_id: uuid.UUID,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(checkout_status_actor_dependency),
) -> FinanceCheckoutStatusResponse:
    raise AssertionError("Finance payment API guard must reject before checkout status reads.")


@router.post("/webhooks/razorpay", status_code=status.HTTP_202_ACCEPTED)
async def receive_razorpay_webhook(
    request: Request,
    _response: Response,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(webhook_actor_dependency),
) -> dict[str, str]:
    raise AssertionError("Finance payment API guard must reject before webhook intake.")


@router.post("/internal/payment-applications", response_model=FinanceInternalPaymentApplicationResponse)
async def apply_internal_payment(
    _request: FinanceInternalPaymentApplicationRequest,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(internal_payment_application_actor_dependency),
) -> FinanceInternalPaymentApplicationResponse:
    raise AssertionError("Finance payment API guard must reject before internal payment application.")


@router.get("/admin/payments/{payment_id}", response_model=FinanceAdminPaymentStatusResponse)
async def get_admin_payment_status(
    payment_id: uuid.UUID,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(finance_admin_actor_dependency),
) -> FinanceAdminPaymentStatusResponse:
    raise AssertionError("Finance payment API guard must reject before admin payment inspection.")
