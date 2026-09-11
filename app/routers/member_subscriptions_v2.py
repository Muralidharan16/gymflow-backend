import uuid
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, update_session_context
from app.core.deps import Staff, require_org_admin
from app.models.member_subscription_v2 import ModernSubscriptionStatus
from app.schemas.member_subscription_v2 import SubscriptionCreate, SubscriptionListResponse, SubscriptionResponse
from app.finance_core.api.guards import require_finance_checkout_sandbox_enabled
from app.finance_core.api.payment_boundary import get_razorpay_test_mode_config, get_razorpay_test_mode_transport
from app.finance_core.api.schemas import FinanceCheckoutCreateResponse
from app.finance_core.domain.provider_boundary import ProviderCheckoutIntentRequest
from app.finance_core.domain.razorpay_sandbox import RazorpayProviderError, RazorpaySandboxConfig
from app.finance_core.services.member_subscription_checkout import (
    MemberSubscriptionCheckoutConfigurationError,
    SourceBoundMemberSubscriptionCheckoutService,
)
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter, RazorpayTestModeOrdersClient, RazorpayTestModeTransport
from app.services.member_subscription_v2_service import MemberSubscriptionV2Service

router = APIRouter(prefix="/organizations/{org_id}/member-subscriptions", tags=["Modern Subscriptions"])


def _enforce_path_org(org_id: uuid.UUID, staff: Staff) -> None:
    if staff.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this organization")


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
    staff: Staff = Depends(require_org_admin),
    _sandbox_enabled: None = Depends(require_finance_checkout_sandbox_enabled),
    razorpay_config: RazorpaySandboxConfig = Depends(get_razorpay_test_mode_config),
    razorpay_transport: RazorpayTestModeTransport = Depends(get_razorpay_test_mode_transport),
):
    _enforce_path_org(org_id, staff)
    await update_session_context(
        db,
        principal_id=str(staff.id),
        principal_type="owner",
        org_id=str(org_id),
        role=getattr(staff, "role", None) or "owner",
    )
    service = SourceBoundMemberSubscriptionCheckoutService(db)
    try:
        prepared = await service.prepare_local_checkout(organization_id=org_id, subscription_id=subscription_id)
        await db.commit()
    except MemberSubscriptionCheckoutConfigurationError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": str(exc), "message": str(exc)})
    except Exception:
        await db.rollback()
        raise

    client = RazorpayTestModeOrdersClient(config=razorpay_config, transport=razorpay_transport)
    adapter = RazorpaySandboxAdapter(config=razorpay_config, client=client)
    provider_order_ref = prepared.provider_order_ref
    if not provider_order_ref or provider_order_ref.startswith("intent_"):
        try:
            provider_response = await adapter.create_checkout_intent(
                ProviderCheckoutIntentRequest(
                    invoice_id=prepared.finance_invoice_id,
                    amount=prepared.amount,
                    currency_code=prepared.currency_code,
                    idempotency_key=f"member-subscription-checkout:{subscription_id}:razorpay_order",
                )
            )
            if provider_response.provider_order_ref is None:
                raise MemberSubscriptionCheckoutConfigurationError("PROVIDER_ORDER_REQUIRED")
            provider_order_ref = provider_response.provider_order_ref
        except (RazorpayProviderError, MemberSubscriptionCheckoutConfigurationError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "PROVIDER_ORDER_FAILED", "message": str(exc)})

        await update_session_context(
            db,
            principal_id=str(staff.id),
            principal_type="owner",
            org_id=str(org_id),
            role=getattr(staff, "role", None) or "owner",
        )
        try:
            await service.attach_provider_order(
                subscription_id=subscription_id,
                checkout_intent_id=prepared.finance_checkout_intent_id,
                provider_order_ref=provider_order_ref,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return FinanceCheckoutCreateResponse(
        finance_invoice_id=prepared.finance_invoice_id,
        finance_checkout_intent_id=prepared.finance_checkout_intent_id,
        checkout_fields=adapter.checkout_fields(order_id=provider_order_ref).to_browser_payload(),
        display_amount=prepared.amount,
        display_currency=prepared.currency_code,
    )
