from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

from app.finance_core.api.auth import (
    FinancePaymentActor,
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
from app.finance_core.domain.checkout_orchestration import (
    CheckoutPlanSelector,
    CreateCheckoutSessionCommand,
    SafeCheckoutSessionResult,
)
from app.finance_core.domain.payment_application_gate import ApplyConfirmedPaymentCommand, AppliedPaymentResult
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput
from app.finance_core.services.checkout_orchestration import FinanceCheckoutOrchestrationService
from app.finance_core.services.payment_application_gate import FinancePaymentApplicationGateService
from app.finance_core.services.razorpay_webhooks import RazorpayWebhookConfirmationService


router = APIRouter(
    prefix="/api/v1/finance/payments",
    tags=["Finance Payment Boundary"],
)


def get_payment_application_gate_service(
    db: AsyncSession = Depends(get_db),
) -> FinancePaymentApplicationGateService:
    # Phase 6L records the route-to-service boundary only. This dependency must
    # remain unreachable while require_finance_payment_api_enabled is first.
    raise AssertionError("Finance payment API guard must reject before payment application gate construction.")


def build_apply_confirmed_payment_command(
    request: FinanceInternalPaymentApplicationRequest,
) -> ApplyConfirmedPaymentCommand:
    return ApplyConfirmedPaymentCommand(
        payment_id=request.payment_id,
        invoice_id=request.invoice_id,
        amount=request.amount,
        currency_code=request.currency_code,
        idempotency_key=request.idempotency_key,
        internal_actor=request.internal_actor,
        reason=request.reason,
    )


def map_internal_payment_application_response(
    result: AppliedPaymentResult,
) -> FinanceInternalPaymentApplicationResponse:
    return FinanceInternalPaymentApplicationResponse(
        allocation_id=result.allocation_id,
        payment_id=result.payment_id,
        invoice_id=result.invoice_id,
        invoice_status=result.invoice_status,
        allocated_amount=result.allocated_amount,
        replayed=result.replayed,
    )


def get_checkout_orchestration_service(
    db: AsyncSession = Depends(get_db),
) -> FinanceCheckoutOrchestrationService:
    # Phase 6J records the route-to-service boundary only. This dependency must
    # remain unreachable while require_finance_payment_api_enabled is first.
    raise AssertionError("Finance payment API guard must reject before checkout orchestration service construction.")


def get_razorpay_webhook_confirmation_service(
    db: AsyncSession = Depends(get_db),
) -> RazorpayWebhookConfirmationService:
    # Phase 6K records the route-to-service boundary only. This dependency must
    # remain unreachable while require_finance_payment_api_enabled is first.
    raise AssertionError("Finance payment API guard must reject before webhook confirmation service construction.")


def build_razorpay_webhook_input(
    *,
    raw_body: bytes,
    signature: str | None,
    idempotency_key: str | None,
) -> RazorpayWebhookInput:
    return RazorpayWebhookInput(
        raw_body=raw_body,
        signature=signature,
        idempotency_key=idempotency_key,
    )


def build_checkout_session_command(
    request: FinanceCheckoutCreateRequest,
    *,
    actor: FinancePaymentActor,
    idempotency_key: str | None,
) -> CreateCheckoutSessionCommand:
    if not idempotency_key:
        raise ValueError("Finance checkout idempotency key is required.")
    return CreateCheckoutSessionCommand(
        organization_id=actor.organization_id,
        billing_party_id=request.billing_party_id,
        selector=CheckoutPlanSelector(
            plan_code=request.plan_code,
            billing_interval=request.billing_interval,
        ),
        idempotency_key=idempotency_key,
    )


def map_checkout_session_response(result: SafeCheckoutSessionResult) -> FinanceCheckoutCreateResponse:
    return FinanceCheckoutCreateResponse(
        finance_invoice_id=result.finance_invoice_id,
        finance_checkout_intent_id=result.finance_checkout_intent_id,
        checkout_fields=result.checkout_fields,
        display_amount=result.display_amount,
        display_currency=result.display_currency,
    )


@router.post("/checkout-sessions", response_model=FinanceCheckoutCreateResponse)
async def create_checkout_session(
    request: FinanceCheckoutCreateRequest,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    _disabled: None = Depends(require_finance_payment_api_enabled),
    actor: FinancePaymentActor = Depends(checkout_actor_dependency),
    checkout_service: FinanceCheckoutOrchestrationService = Depends(get_checkout_orchestration_service),
) -> FinanceCheckoutCreateResponse:
    command = build_checkout_session_command(request, actor=actor, idempotency_key=x_idempotency_key)
    result = await checkout_service.create_checkout_session(command)
    return map_checkout_session_response(result)


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
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(webhook_actor_dependency),
    webhook_service: RazorpayWebhookConfirmationService = Depends(get_razorpay_webhook_confirmation_service),
) -> dict[str, str]:
    webhook = build_razorpay_webhook_input(
        raw_body=await request.body(),
        signature=x_razorpay_signature,
        idempotency_key=x_idempotency_key,
    )
    await webhook_service.confirm_payment_event(webhook)
    return {"status": "accepted"}


@router.post("/internal/payment-applications", response_model=FinanceInternalPaymentApplicationResponse)
async def apply_internal_payment(
    request: FinanceInternalPaymentApplicationRequest,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(internal_payment_application_actor_dependency),
    application_gate: FinancePaymentApplicationGateService = Depends(get_payment_application_gate_service),
) -> FinanceInternalPaymentApplicationResponse:
    command = build_apply_confirmed_payment_command(request)
    result = await application_gate.apply_confirmed_payment(command)
    return map_internal_payment_application_response(result)


@router.get("/admin/payments/{payment_id}", response_model=FinanceAdminPaymentStatusResponse)
async def get_admin_payment_status(
    payment_id: uuid.UUID,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(finance_admin_actor_dependency),
) -> FinanceAdminPaymentStatusResponse:
    raise AssertionError("Finance payment API guard must reject before admin payment inspection.")
