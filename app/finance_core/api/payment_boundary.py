from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response, status
from sqlalchemy.exc import DBAPIError
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
from app.finance_core.api.guards import (
    require_finance_checkout_sandbox_enabled,
    require_finance_internal_apply_sandbox_enabled,
    require_finance_payment_api_enabled,
    require_finance_webhook_sandbox_enabled,
)
from app.finance_core.api.schemas import (
    FinanceAdminPaymentStatusResponse,
    FinanceCheckoutCreateRequest,
    FinanceCheckoutCreateResponse,
    FinanceCheckoutStatusResponse,
    FinanceInternalPaymentApplicationRequest,
    FinanceInternalPaymentApplicationResponse,
)
from app.finance_core.domain.checkout_orchestration import (
    CheckoutPlanResolver,
    CheckoutPlanSelector,
    CreateCheckoutSessionCommand,
    SafeCheckoutSessionResult,
)
from app.finance_core.domain.invoice_engine import FinanceInvoiceNotFoundError, FinanceInvoiceStateError
from app.finance_core.domain.operational_guards import FinanceOperationalGuardError
from app.finance_core.domain.payment_application_gate import (
    AppliedPaymentResult,
    ApplyConfirmedPaymentCommand,
    FinancePaymentApplicationAuthorityError,
)
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    FinancePaymentNotFoundError,
    FinancePaymentStateError,
)
from app.finance_core.domain.provider_capture_confirmation import FinanceProviderEvidenceError
from app.finance_core.domain.provider_boundary import (
    CheckoutProviderRegistry,
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    FinancePaymentStateTransitionError,
    ProviderSandboxConfig,
    validate_sandbox_provider_config,
    FinanceWebhookNormalizationError,
    FinanceWebhookSignatureError,
)
from app.finance_core.domain.razorpay_sandbox import RazorpayProviderError, RazorpaySandboxConfig, validate_razorpay_sandbox_config
from app.finance_core.domain.razorpay_webhooks import RazorpayWebhookInput
from app.finance_core.services.checkout_orchestration import FinanceCheckoutOrchestrationService
from app.finance_core.services.payment_application_gate import FinancePaymentApplicationGateService
from app.finance_core.services.razorpay_sandbox import (
    RazorpaySandboxAdapter,
    RazorpayTestModeOrdersClient,
    RazorpayTestModeTransport,
)
from app.finance_core.services.razorpay_webhooks import RazorpayWebhookConfirmationService


router = APIRouter(
    prefix="/api/v1/finance/payments",
    tags=["Finance Payment Boundary"],
)


def get_payment_application_gate_service(
    db: AsyncSession = Depends(get_db),
) -> FinancePaymentApplicationGateService:
    return FinancePaymentApplicationGateService(db)


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


def get_checkout_plan_resolver() -> CheckoutPlanResolver:
    raise AssertionError("Finance checkout plan resolver must be explicitly injected for sandbox checkout.")


def get_razorpay_test_mode_config() -> RazorpaySandboxConfig:
    raise AssertionError("Razorpay test-mode config must be explicitly injected for sandbox checkout.")


def get_razorpay_test_mode_transport() -> RazorpayTestModeTransport:
    raise AssertionError("Razorpay test-mode transport must be explicitly injected for sandbox checkout.")


def get_checkout_orchestration_service(
    db: AsyncSession = Depends(get_db),
    plan_resolver: CheckoutPlanResolver = Depends(get_checkout_plan_resolver),
    razorpay_config: RazorpaySandboxConfig = Depends(get_razorpay_test_mode_config),
    razorpay_transport: RazorpayTestModeTransport = Depends(get_razorpay_test_mode_transport),
) -> FinanceCheckoutOrchestrationService:
    try:
        client = RazorpayTestModeOrdersClient(
            config=razorpay_config,
            transport=razorpay_transport,
        )
        adapter = RazorpaySandboxAdapter(
            config=razorpay_config,
            client=client,
        )
    except (FinanceProviderConfigError, RazorpayProviderError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FINANCE_CHECKOUT_PROVIDER_CONFIG_UNSAFE",
                "message": "Finance checkout provider configuration is unsafe.",
            },
        )
    registry = CheckoutProviderRegistry((adapter,))
    provider_adapter = registry.resolve(adapter.provider_code)
    return FinanceCheckoutOrchestrationService(
        db,
        plan_resolver=plan_resolver,
        provider_adapter=provider_adapter,
    )


def get_razorpay_webhook_test_mode_config() -> RazorpaySandboxConfig:
    raise AssertionError("Razorpay webhook test-mode config must be explicitly injected for sandbox webhook.")


def get_provider_webhook_sandbox_config() -> ProviderSandboxConfig:
    raise AssertionError("Provider webhook sandbox config must be explicitly injected for sandbox webhook.")


def get_razorpay_webhook_confirmation_service(
    db: AsyncSession = Depends(get_db),
    razorpay_config: RazorpaySandboxConfig = Depends(get_razorpay_webhook_test_mode_config),
    provider_config: ProviderSandboxConfig = Depends(get_provider_webhook_sandbox_config),
) -> RazorpayWebhookConfirmationService:
    try:
        safe_razorpay_config = validate_razorpay_sandbox_config(razorpay_config)
        safe_provider_config = validate_sandbox_provider_config(provider_config)
    except FinanceProviderConfigError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FINANCE_WEBHOOK_PROVIDER_CONFIG_UNSAFE",
                "message": "Finance webhook provider configuration is unsafe.",
            },
        )
    return RazorpayWebhookConfirmationService(
        db,
        razorpay_config=safe_razorpay_config,
        provider_config=safe_provider_config,
    )


def build_razorpay_webhook_input(
    *,
    raw_body: bytes,
    signature: str | None,
    provider_event_id: str | None = None,
    idempotency_key: str | None = None,
) -> RazorpayWebhookInput:
    return RazorpayWebhookInput(
        raw_body=raw_body,
        signature=signature,
        provider_event_id=provider_event_id,
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


def _provider_operation_http_error(
    exc: FinanceProviderOperationError,
) -> HTTPException:
    if exc.requires_reconciliation:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "FINANCE_PROVIDER_OUTCOME_UNKNOWN",
                "message": (
                    "Provider outcome is unknown and requires "
                    "reconciliation before retry."
                ),
            },
        )
    if exc.automatic_retry_allowed:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCE_PROVIDER_RETRYABLE_FAILURE",
                "message": "Provider is temporarily unavailable; retry is safe.",
            },
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "FINANCE_PROVIDER_FINAL_FAILURE",
            "message": "Provider rejected the checkout operation.",
        },
    )


@router.post("/checkout-sessions", response_model=FinanceCheckoutCreateResponse)
async def create_checkout_session(
    request: FinanceCheckoutCreateRequest,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    actor: FinancePaymentActor = Depends(checkout_actor_dependency),
    _sandbox_enabled: None = Depends(require_finance_checkout_sandbox_enabled),
    db: AsyncSession = Depends(get_db),
    checkout_service: FinanceCheckoutOrchestrationService = Depends(
        get_checkout_orchestration_service
    ),
) -> FinanceCheckoutCreateResponse:
    command = build_checkout_session_command(
        request,
        actor=actor,
        idempotency_key=x_idempotency_key,
    )

    prepared = await checkout_service.prepare_checkout_session(command)
    # Durable local authority exists before any provider I/O.
    await db.commit()

    if prepared.provider_order_id:
        return map_checkout_session_response(
            checkout_service.build_result(
                prepared,
                provider_order_id=prepared.provider_order_id,
            )
        )
    if prepared.provider_operation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "FINANCE_PROVIDER_OPERATION_UNAVAILABLE",
                "message": "Provider operation is unavailable.",
            },
        )
    if prepared.provider_operation.status not in (
        "reserved",
        "failed_retryable",
    ):
        raise _provider_operation_http_error(
            checkout_service.operation_state_error(
                prepared.provider_operation.status
            )
        )

    lease_owner = uuid.uuid4()
    claim = await checkout_service.claim_provider_operation(
        prepared,
        lease_owner=lease_owner,
    )
    # Commit the in-flight lease/fence before submitting the external POST.
    await db.commit()

    if not claim.claimed:
        if claim.status == "succeeded" and claim.provider_object_id:
            return map_checkout_session_response(
                checkout_service.build_result(
                    prepared,
                    provider_order_id=claim.provider_object_id,
                )
            )
        raise _provider_operation_http_error(
            checkout_service.operation_state_error(claim.status)
        )

    try:
        response = await checkout_service.call_provider(prepared)
    except FinanceProviderOperationError as exc:
        await checkout_service.finish_provider_error(
            claim,
            lease_owner=lease_owner,
            error=exc,
        )
        # Persist retryable/final/unknown before returning the HTTP result.
        await db.commit()
        raise _provider_operation_http_error(exc) from exc

    # If this DB acknowledgement fails after provider success, the already
    # committed in-flight lease later expires to UNKNOWN. A retry cannot submit
    # a second create until reconciliation resolves the ambiguity.
    provider_order_id = await checkout_service.finish_provider_success(
        prepared,
        claim,
        lease_owner=lease_owner,
        response=response,
    )
    await db.commit()

    return map_checkout_session_response(
        checkout_service.build_result(
            prepared,
            provider_order_id=provider_order_id,
        )
    )


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
    x_razorpay_event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    _actor: None = Depends(webhook_actor_dependency),
    _sandbox_enabled: None = Depends(require_finance_webhook_sandbox_enabled),
    db: AsyncSession = Depends(get_db),
    webhook_service: RazorpayWebhookConfirmationService = Depends(
        get_razorpay_webhook_confirmation_service
    ),
) -> dict[str, str]:
    webhook = build_razorpay_webhook_input(
        raw_body=await request.body(),
        signature=x_razorpay_signature,
        provider_event_id=x_razorpay_event_id,
        idempotency_key=x_idempotency_key,
    )

    try:
        receipt = await webhook_service.record_verified_webhook(webhook)
        # A verified normalized inbox row is durable before Finance state changes.
        await db.commit()
    except FinanceWebhookSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "FINANCE_WEBHOOK_SIGNATURE_INVALID",
                "message": "Webhook signature is invalid.",
            },
        ) from exc
    except FinanceWebhookNormalizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "FINANCE_WEBHOOK_PAYLOAD_INVALID",
                "message": "Webhook payload is invalid.",
            },
        ) from exc

    if receipt.status == "processed":
        return {"status": "accepted"}
    if receipt.status == "dead_letter":
        # Durable terminal evidence already exists; acknowledge replay without
        # re-running a known-invalid financial transition.
        return {"status": "accepted"}

    lease_owner = uuid.uuid4()
    claimed = await webhook_service.claim_recorded_webhook(
        inbox_id=receipt.inbox_id,
        lease_owner=lease_owner,
    )
    # Claim/fence is durable before Finance evidence application.
    await db.commit()

    if not claimed.claimed:
        return {
            "status": (
                "accepted"
                if claimed.status in {"processed", "dead_letter"}
                else "processing"
            )
        }

    try:
        result = await webhook_service.process_claimed_webhook(claimed)
        await webhook_service.complete_claimed_webhook(
            claimed=claimed,
            lease_owner=lease_owner,
            payment_event_id=result.payment_event_id,
        )
        # Payment state + payment_event + outbox + inbox completion commit
        # together. Crash before commit leaves the durable inbox reclaimable.
        await db.commit()
    except FinanceProviderEvidenceError as exc:
        await db.rollback()
        await webhook_service.fail_claimed_webhook(
            claimed=claimed,
            lease_owner=lease_owner,
            error_code="provider_evidence_invalid",
            retryable=False,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "FINANCE_WEBHOOK_PAYLOAD_INVALID",
                "message": "Webhook payload is invalid.",
            },
        ) from exc
    except (FinancePaymentStateTransitionError, FinancePaymentConflictError) as exc:
        await db.rollback()
        await webhook_service.fail_claimed_webhook(
            claimed=claimed,
            lease_owner=lease_owner,
            error_code="provider_state_conflict",
            retryable=False,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "FINANCE_WEBHOOK_STATE_CONFLICT",
                "message": "Webhook state transition is invalid.",
            },
        ) from exc
    except (DBAPIError, FinanceOperationalGuardError):
        await db.rollback()
        await webhook_service.fail_claimed_webhook(
            claimed=claimed,
            lease_owner=lease_owner,
            error_code="provider_processing_retry",
            retryable=True,
        )
        await db.commit()
        # The provider delivery is durably accepted. Recovery can be driven by
        # a later provider replay or the dedicated finance-payment capability.
        return {"status": "queued"}

    return {"status": "accepted"}


@router.post("/internal/payment-applications", response_model=FinanceInternalPaymentApplicationResponse)
async def apply_internal_payment(
    request: FinanceInternalPaymentApplicationRequest,
    _sandbox_enabled: None = Depends(require_finance_internal_apply_sandbox_enabled),
    _actor: None = Depends(internal_payment_application_actor_dependency),
    application_gate: FinancePaymentApplicationGateService = Depends(get_payment_application_gate_service),
) -> FinanceInternalPaymentApplicationResponse:
    command = build_apply_confirmed_payment_command(request)
    try:
        result = await application_gate.apply_confirmed_payment(command)
    except FinancePaymentApplicationAuthorityError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FINANCE_INTERNAL_APPLY_FORBIDDEN", "message": "Payment application authority is invalid."},
        )
    except (FinancePaymentNotFoundError, FinanceInvoiceNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "FINANCE_INTERNAL_APPLY_NOT_FOUND", "message": "Payment application target was not found."},
        )
    except FinanceOperationalGuardError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FINANCE_INTERNAL_APPLY_GUARD_UNSAFE", "message": "Payment application guard posture is unsafe."},
        )
    except (FinancePaymentConflictError, FinancePaymentStateError, FinanceInvoiceStateError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "FINANCE_INTERNAL_APPLY_STATE_CONFLICT", "message": "Payment application state is invalid."},
        )
    return map_internal_payment_application_response(result)


@router.get("/admin/payments/{payment_id}", response_model=FinanceAdminPaymentStatusResponse)
async def get_admin_payment_status(
    payment_id: uuid.UUID,
    _disabled: None = Depends(require_finance_payment_api_enabled),
    _actor: None = Depends(finance_admin_actor_dependency),
) -> FinanceAdminPaymentStatusResponse:
    raise AssertionError("Finance payment API guard must reject before admin payment inspection.")
