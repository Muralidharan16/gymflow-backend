import uuid
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, update_session_context
from app.core.deps import Staff, require_org_admin
from app.core.payment_database import get_finance_payment_db
from app.models.member_subscription_v2 import ModernSubscriptionStatus
from app.schemas.member_subscription_v2 import SubscriptionCreate, SubscriptionListResponse, SubscriptionResponse
from app.finance_core.api.guards import require_finance_checkout_sandbox_enabled
from app.finance_core.api.payment_boundary import (
    PAY24B_CHECKOUT_ADMISSION_LEASE_SECONDS,
    get_razorpay_test_mode_config,
    get_razorpay_test_mode_transport,
)
from app.finance_core.api.schemas import FinanceCheckoutCreateResponse
from app.finance_core.domain.provider_boundary import (
    CheckoutProviderRegistry,
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
)
from app.finance_core.domain.razorpay_sandbox import RazorpaySandboxConfig
from app.finance_core.services.member_subscription_checkout import (
    MemberSubscriptionCheckoutConfigurationError,
    SourceBoundMemberSubscriptionCheckoutService,
)
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter, RazorpayTestModeOrdersClient, RazorpayTestModeTransport
from app.payment_activation import ActivationCapability, DurableActivationAuthority
from app.services.member_subscription_v2_service import MemberSubscriptionV2Service

router = APIRouter(prefix="/organizations/{org_id}/member-subscriptions", tags=["Modern Subscriptions"])


def _enforce_path_org(org_id: uuid.UUID, staff: Staff) -> None:
    if staff.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this organization")


def _pay24b_member_checkout_admission_http_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "FINANCE_PROVIDER_EGRESS_NOT_AUTHORIZED",
            "message": "Provider checkout egress is not currently authorized.",
        },
    )


@router.post("", response_model=SubscriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    org_id: uuid.UUID,
    data: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    try:
        subscription = await service.create_subscription(org_id, data, staff.id)
        await db.commit()
        return subscription
    except Exception:
        await db.rollback()
        raise


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    org_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status_filter: ModernSubscriptionStatus | None = Query(None, alias="status"),
    branch_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    subscriptions, total = await service.list_subscriptions(
        org_id=org_id,
        page=page,
        size=page_size,
        status_filter=status_filter,
        branch_id=branch_id,
        member_id=member_id,
    )
    return SubscriptionListResponse(
        data=[SubscriptionResponse.model_validate(sub) for sub in subscriptions],
        total=total,
        page=page,
        size=page_size,
        pages=ceil(total / page_size) if page_size else 0,
    )


@router.get("/{subscription_id}", response_model=SubscriptionResponse)
async def get_subscription(
    org_id: uuid.UUID,
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    staff: Staff = Depends(require_org_admin),
):
    _enforce_path_org(org_id, staff)
    service = MemberSubscriptionV2Service(db)
    subscription = await service.get_subscription(org_id, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return subscription


@router.post("/{subscription_id}/checkout-session", response_model=FinanceCheckoutCreateResponse)
async def create_subscription_checkout_session(
    org_id: uuid.UUID,
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payment_db: AsyncSession = Depends(get_finance_payment_db),
    staff: Staff = Depends(require_org_admin),
    _sandbox_enabled: None = Depends(require_finance_checkout_sandbox_enabled),
    razorpay_config: RazorpaySandboxConfig = Depends(get_razorpay_test_mode_config),
    razorpay_transport: RazorpayTestModeTransport = Depends(get_razorpay_test_mode_transport),
):
    _enforce_path_org(org_id, staff)
    client = RazorpayTestModeOrdersClient(
        config=razorpay_config,
        transport=razorpay_transport,
    )
    razorpay = RazorpaySandboxAdapter(config=razorpay_config, client=client)
    registry = CheckoutProviderRegistry((razorpay,))
    provider_adapter = registry.resolve(razorpay.provider_code)

    await update_session_context(
        db,
        principal_id=str(staff.id),
        principal_type="owner",
        org_id=str(org_id),
        role=getattr(staff, "role", None) or "owner",
    )
    service = SourceBoundMemberSubscriptionCheckoutService(db)
    try:
        prepared = await service.prepare_local_checkout(
            organization_id=org_id,
            subscription_id=subscription_id,
            provider_code=provider_adapter.provider_code,
            provider_environment=provider_adapter.environment,
        )
        # Persist local invoice/binding/payment intent/provider reservation first.
        await db.commit()
    except MemberSubscriptionCheckoutConfigurationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(exc), "message": str(exc)},
        ) from exc
    except Exception:
        await db.rollback()
        raise

    if prepared.provider_order_ref:
        result = service.build_result(
            prepared=prepared,
            provider_adapter=provider_adapter,
            provider_order_ref=prepared.provider_order_ref,
        )
        return FinanceCheckoutCreateResponse(
            finance_invoice_id=result.finance_invoice_id,
            finance_checkout_intent_id=result.finance_checkout_intent_id,
            checkout_fields=result.checkout_fields,
            display_amount=result.display_amount,
            display_currency=result.display_currency,
        )

    if prepared.provider_operation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROVIDER_OPERATION_UNAVAILABLE",
                "message": "Provider operation is unavailable.",
            },
        )
    if prepared.provider_operation.status not in (
        "reserved",
        "failed_retryable",
    ):
        exc = service.operation_state_error(
            provider_code=provider_adapter.provider_code,
            status_value=prepared.provider_operation.status,
        )
        if exc.requires_reconciliation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "PROVIDER_OUTCOME_UNKNOWN",
                    "message": "Provider outcome requires reconciliation before retry.",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROVIDER_FINAL_FAILURE",
                "message": "Provider checkout operation is terminal.",
            },
        ) from exc

    payment_effects = service.bind_provider_effects(payment_db)
    lease_owner = uuid.uuid4()
    authority = DurableActivationAuthority(payment_db)
    admission = None
    try:
        claim = await payment_effects.claim_provider_operation(
            prepared=prepared,
            lease_owner=lease_owner,
        )
        if claim.claimed:
            binding = service.provider_admission_binding(
                prepared=prepared,
                claim=claim,
            )
            admission = await authority.request_current_provider_admission(
                capability=ActivationCapability.CHECKOUT,
                logical_operation_id=binding.logical_operation_id,
                operation_sha=binding.operation_sha,
                lease_seconds=PAY24B_CHECKOUT_ADMISSION_LEASE_SECONDS,
            )
        # P1: PAY8 claim and PAY24 admission become durable together.
        await payment_db.commit()
    except DBAPIError as exc:
        await payment_db.rollback()
        raise _pay24b_member_checkout_admission_http_error() from exc

    if not claim.claimed:
        if claim.status == "succeeded" and claim.provider_object_id:
            result = service.build_result(
                prepared=prepared,
                provider_adapter=provider_adapter,
                provider_order_ref=claim.provider_object_id,
            )
            return FinanceCheckoutCreateResponse(
                finance_invoice_id=result.finance_invoice_id,
                finance_checkout_intent_id=result.finance_checkout_intent_id,
                checkout_fields=result.checkout_fields,
                display_amount=result.display_amount,
                display_currency=result.display_currency,
            )
        exc = service.operation_state_error(
            provider_code=provider_adapter.provider_code,
            status_value=claim.status,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": (
                    "PROVIDER_OUTCOME_UNKNOWN"
                    if exc.requires_reconciliation
                    else "PROVIDER_FINAL_FAILURE"
                ),
                "message": (
                    "Provider outcome requires reconciliation before retry."
                    if exc.requires_reconciliation
                    else "Provider checkout operation is terminal."
                ),
            },
        ) from exc

    if admission is None:
        raise _pay24b_member_checkout_admission_http_error()

    try:
        started_admission = await authority.start_provider_admission(
            admission_id=admission.admission_id,
            execution_id=lease_owner,
        )
        # P2: the active admission fence is durable before provider I/O.
        await payment_db.commit()
    except DBAPIError as exc:
        await payment_db.rollback()
        no_effect = FinanceProviderOperationError(
            provider_code="activation",
            operation="create_checkout",
            code="PAY24_ADMISSION_START_DENIED",
            failure_class="retryable",
            message="Provider admission was denied before provider I/O.",
        )
        await payment_effects.finish_provider_error(
            claim=claim,
            lease_owner=lease_owner,
            error=no_effect,
        )
        await payment_db.commit()
        raise _pay24b_member_checkout_admission_http_error() from exc

    if started_admission.state != "active":
        no_effect = FinanceProviderOperationError(
            provider_code="activation",
            operation="create_checkout",
            code="PAY24_ADMISSION_NOT_ACTIVE",
            failure_class="retryable",
            message="Provider admission was not active before provider I/O.",
        )
        await payment_effects.finish_provider_error(
            claim=claim,
            lease_owner=lease_owner,
            error=no_effect,
        )
        await payment_db.commit()
        raise _pay24b_member_checkout_admission_http_error()

    try:
        response = await service.call_provider(
            prepared=prepared,
            provider_adapter=provider_adapter,
        )
        provider_order_ref = await payment_effects.finish_provider_success(
            provider_adapter=provider_adapter,
            claim=claim,
            lease_owner=lease_owner,
            response=response,
        )
    except FinanceProviderOperationError as exc:
        await payment_effects.finish_provider_error(
            claim=claim,
            lease_owner=lease_owner,
            error=exc,
        )
        await authority.finish_provider_admission(
            admission_id=admission.admission_id,
            execution_id=lease_owner,
            outcome=("unknown" if exc.requires_reconciliation else "completed"),
        )
        # P3: PAY8 and PAY24 terminal outcomes become durable atomically.
        await payment_db.commit()
        if exc.requires_reconciliation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "PROVIDER_OUTCOME_UNKNOWN",
                    "message": "Provider outcome requires reconciliation before retry.",
                },
            ) from exc
        if exc.automatic_retry_allowed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "PROVIDER_RETRYABLE_FAILURE",
                    "message": "Provider is temporarily unavailable; retry is safe.",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROVIDER_FINAL_FAILURE",
                "message": "Provider rejected the checkout operation.",
            },
        ) from exc

    await authority.finish_provider_admission(
        admission_id=admission.admission_id,
        execution_id=lease_owner,
        outcome="completed",
    )
    await payment_db.commit()

    result = service.build_result(
        prepared=prepared,
        provider_adapter=provider_adapter,
        provider_order_ref=provider_order_ref,
    )
    return FinanceCheckoutCreateResponse(
        finance_invoice_id=result.finance_invoice_id,
        finance_checkout_intent_id=result.finance_checkout_intent_id,
        checkout_fields=result.checkout_fields,
        display_amount=result.display_amount,
        display_currency=result.display_currency,
    )
