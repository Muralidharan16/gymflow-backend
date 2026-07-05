from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.platform_billing.api.schemas import (
    FakeCheckoutSimulationAvailability,
    PlatformBillingActionOption,
    PlatformBillingCheckoutAvailability,
    PlatformBillingCheckoutOptionsResponse,
    PlatformBillingCurrentSubscriptionOption,
    PlatformBillingDiagnostics,
    PlatformBillingPlanOption,
    PlatformBillingPriceOption,
)
from app.platform_billing.domain.capability import OperationClass
from app.platform_billing.domain.hashing import CanonicalSerializer
from app.platform_billing.domain.read_models import PlanVersionRead, PriceRead
from app.platform_billing.providers.fake_checkout_evidence import (
    FakeCheckoutEvidenceError,
    SUPPORTED_OUTCOMES,
    default_fake_checkout_evidence_store,
)
from app.platform_billing.repositories.catalog import PlatformCatalogReadRepository
from app.platform_billing.services.capability_authorization_service import CapabilityAuthorizationService
from app.platform_billing.services.checkout import (
    effective_prices_for_plan,
    get_active_fake_provider_customer,
)
from app.platform_billing.services.query_service import PlatformBillingQueryService, SubscriptionDetailRead


ACTION_CODE_START_SUBSCRIPTION = "start_subscription"
FAKE_SIMULATION_WARNING = "Development test simulation. No real payment is performed. No subscription is activated."
FAKE_CHECKOUT_ALLOWED_OUTCOMES = ["pending", "succeeded", "failed"]

REASON_MESSAGES = {
    "ACTION_NOT_PERMITTED": "You can view Platform Billing, but cannot start a subscription.",
    "CHECKOUT_FEATURE_DISABLED": "Platform Billing checkout is not currently available.",
    "ENVIRONMENT_DENIED": "Platform Billing checkout is not available in this environment.",
    "PROVIDER_MODE_UNAVAILABLE": "Platform Billing checkout provider mode is not available.",
    "PROVIDER_CUSTOMER_MISSING": "Platform Billing checkout is not ready for this organization.",
    "CURRENT_SUBSCRIPTION_EXISTS": "A current Platform Billing subscription already exists.",
    "ACTIVE_SUBSCRIPTION_EXISTS": "An active Platform Billing subscription already exists.",
    "TRIAL_SUBSCRIPTION_EXISTS": "A trial Platform Billing subscription already exists.",
    "CANCELLATION_SCHEDULED": "A Platform Billing subscription cancellation is already scheduled.",
    "NO_AVAILABLE_PLANS": "No Platform Billing checkout option is currently available.",
    "CATALOG_TERMS_UNAVAILABLE": "Platform Billing catalog terms are not currently available.",
    "CATALOG_PRICE_AMBIGUOUS": "Platform Billing catalog prices are not currently unambiguous.",
}


class PlatformBillingCheckoutOptionsService:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._query = PlatformBillingQueryService(db)
        self._catalog = PlatformCatalogReadRepository(db)

    async def get_checkout_options(
        self,
        *,
        organization_id: uuid.UUID,
    ) -> PlatformBillingCheckoutOptionsResponse:
        now = datetime.now(timezone.utc)
        subscription_detail = await self._query.get_current_subscription(organization_id)
        published_plans = await self._catalog.list_published_plan_versions(now=now)
        plans, has_ambiguous_price = _plan_options(published_plans, subscription_detail, now)
        change_plan_allowed = await self._change_plan_allowed(organization_id)
        provider_customer_ready = await get_active_fake_provider_customer(self._db, organization_id) is not None

        availability_reason = _availability_reason(
            subscription_detail=subscription_detail,
            plans=plans,
            has_ambiguous_price=has_ambiguous_price,
            change_plan_allowed=change_plan_allowed,
            provider_customer_ready=provider_customer_ready,
        )
        available = availability_reason is None
        actions = _action_options(
            plans=plans,
            available=available,
            reason_code=availability_reason,
        )

        return PlatformBillingCheckoutOptionsResponse(
            server_time=now,
            catalog_version=_catalog_version(plans),
            current_subscription=await self._current_subscription_option(subscription_detail, now),
            plans=plans,
            actions=actions,
            checkout_availability=PlatformBillingCheckoutAvailability(
                available=available,
                reason_code=availability_reason,
                message=_message_for_reason(availability_reason),
                action_code=ACTION_CODE_START_SUBSCRIPTION,
            ),
            diagnostics=PlatformBillingDiagnostics(
                fake_checkout_simulation=await self._fake_simulation_availability(
                    change_plan_allowed=change_plan_allowed,
                    provider_customer_ready=provider_customer_ready,
                )
            ),
        )

    async def _current_subscription_option(
        self,
        subscription_detail: SubscriptionDetailRead | None,
        now: datetime,
    ) -> PlatformBillingCurrentSubscriptionOption:
        if subscription_detail is None:
            return PlatformBillingCurrentSubscriptionOption()

        sub = subscription_detail.subscription
        plan = await self._query.get_plan_detail(sub.current_plan_version_id, now=now)
        open_period = next((period for period in subscription_detail.periods if period.status == "open"), None)
        return PlatformBillingCurrentSubscriptionOption(
            status=sub.status,
            current_plan_code=plan.code if plan else None,
            current_plan_display_name=plan.display_name if plan else None,
            period_type=open_period.period_type if open_period else None,
            cancel_at_period_end=sub.cancel_at_period_end,
        )

    async def _change_plan_allowed(self, organization_id: uuid.UUID) -> bool:
        raw_enforcement_enabled = settings.PLATFORM_BILLING_ENFORCEMENT is True
        raw_shadow_enabled = settings.PLATFORM_BILLING_SHADOW_RESOLVER is True
        if not raw_enforcement_enabled and not raw_shadow_enabled:
            return True

        service = CapabilityAuthorizationService(self._db)
        result = await service.authorize(
            organization_id=organization_id,
            capability_key="platform_billing.change_plan",
            operation_class=OperationClass.financial.value,
        )
        return result.decision.allowed

    async def _fake_simulation_availability(
        self,
        *,
        change_plan_allowed: bool,
        provider_customer_ready: bool,
    ) -> FakeCheckoutSimulationAvailability:
        allowed_outcomes = _ordered_fake_outcomes()
        available = (
            change_plan_allowed
            and settings.PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED is True
            and settings.PLATFORM_BILLING_FAKE_CHECKOUT_SIMULATION_ENABLED is True
            and settings.ENVIRONMENT in ("development", "test")
            and settings.PLATFORM_BILLING_PROVIDER_MODE == "fake"
            and provider_customer_ready
            and _fake_evidence_store_usable()
        )
        return FakeCheckoutSimulationAvailability(
            available=available,
            allowed_outcomes=allowed_outcomes if available else [],
            warning=FAKE_SIMULATION_WARNING,
        )


def _plan_options(
    plans: list[PlanVersionRead],
    subscription_detail: SubscriptionDetailRead | None,
    now: datetime,
) -> tuple[list[PlatformBillingPlanOption], bool]:
    current_plan_id = subscription_detail.subscription.current_plan_version_id if subscription_detail else None
    options: list[PlatformBillingPlanOption] = []
    has_ambiguous_price = False

    for plan in plans:
        prices_by_interval: dict[str, list[PriceRead]] = {}
        for price in effective_prices_for_plan(plan, billing_interval=None, now=now):
            prices_by_interval.setdefault(price.billing_interval, []).append(price)

        price_options: list[PlatformBillingPriceOption] = []
        for _interval, prices in sorted(prices_by_interval.items()):
            if len(prices) != 1:
                has_ambiguous_price = True
                continue
            price = prices[0]
            price_options.append(
                PlatformBillingPriceOption(
                    billing_interval=price.billing_interval,
                    interval_count=price.interval_count,
                    amount_minor=price.money.amount_minor,
                    currency=price.money.currency_code,
                    tax_behavior=price.tax_behavior,
                )
            )

        if not price_options:
            continue
        options.append(
            PlatformBillingPlanOption(
                plan_code=plan.code,
                display_name=plan.display_name,
                description=plan.description,
                is_current=plan.id == current_plan_id,
                prices=price_options,
                feature_summary=[],
            )
        )

    return options, has_ambiguous_price


def _availability_reason(
    *,
    subscription_detail: SubscriptionDetailRead | None,
    plans: list[PlatformBillingPlanOption],
    has_ambiguous_price: bool,
    change_plan_allowed: bool,
    provider_customer_ready: bool,
) -> str | None:
    if not change_plan_allowed:
        return "ACTION_NOT_PERMITTED"
    if settings.PLATFORM_BILLING_FAKE_CHECKOUT_ENABLED is not True:
        return "CHECKOUT_FEATURE_DISABLED"
    if settings.ENVIRONMENT not in ("development", "test"):
        return "ENVIRONMENT_DENIED"
    if settings.PLATFORM_BILLING_PROVIDER_MODE != "fake":
        return "PROVIDER_MODE_UNAVAILABLE"
    if subscription_detail is not None:
        return _subscription_reason(subscription_detail)
    if has_ambiguous_price:
        return "CATALOG_PRICE_AMBIGUOUS"
    if not plans:
        return "NO_AVAILABLE_PLANS"
    if not provider_customer_ready:
        return "PROVIDER_CUSTOMER_MISSING"
    return None


def _subscription_reason(subscription_detail: SubscriptionDetailRead) -> str:
    sub = subscription_detail.subscription
    if sub.cancel_at_period_end or sub.status == "cancel_scheduled":
        return "CANCELLATION_SCHEDULED"
    if sub.status == "active":
        return "ACTIVE_SUBSCRIPTION_EXISTS"
    if sub.status == "trialing":
        return "TRIAL_SUBSCRIPTION_EXISTS"
    return "CURRENT_SUBSCRIPTION_EXISTS"


def _action_options(
    *,
    plans: list[PlatformBillingPlanOption],
    available: bool,
    reason_code: str | None,
) -> list[PlatformBillingActionOption]:
    actions: list[PlatformBillingActionOption] = []
    for plan in plans:
        for price in plan.prices:
            actions.append(
                PlatformBillingActionOption(
                    action_code=ACTION_CODE_START_SUBSCRIPTION,
                    target_plan_code=plan.plan_code,
                    billing_interval=price.billing_interval,
                    display_label="Start subscription",
                    is_available=available,
                    unavailable_reason_code=None if available else reason_code,
                    checkout_supported=available,
                    requires_confirmation=True,
                )
            )
    return actions


def _catalog_version(plans: list[PlatformBillingPlanOption]) -> str:
    payload = [
        {
            "plan_code": plan.plan_code,
            "display_name": plan.display_name,
            "prices": [
                {
                    "amount_minor": price.amount_minor,
                    "billing_interval": price.billing_interval,
                    "currency": price.currency,
                    "interval_count": price.interval_count,
                    "tax_behavior": price.tax_behavior,
                }
                for price in plan.prices
            ],
        }
        for plan in plans
    ]
    digest = hashlib.sha256(CanonicalSerializer.serialize(payload).encode("utf-8")).hexdigest()
    return f"platform-catalog-sha256:{digest}"


def _message_for_reason(reason_code: str | None) -> str:
    if reason_code is None:
        return "Platform Billing checkout is available."
    return REASON_MESSAGES.get(reason_code, "Platform Billing checkout is not currently available.")


def _fake_evidence_store_usable() -> bool:
    try:
        default_fake_checkout_evidence_store()._validate_root()
        return True
    except FakeCheckoutEvidenceError:
        return False


def _ordered_fake_outcomes() -> list[str]:
    if set(FAKE_CHECKOUT_ALLOWED_OUTCOMES) != set(SUPPORTED_OUTCOMES):
        return []
    return list(FAKE_CHECKOUT_ALLOWED_OUTCOMES)
