from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.platform_billing.api.schemas import (
    CreateCheckoutSessionRequest,
    CreateCheckoutSessionResponse,
    GetCheckoutOperationResponse,
)
from app.platform_billing.domain.provider_operations import (
    IdempotencyConflict,
    ProviderOperationConflict,
    ProviderOperationRequest,
    ProviderOperationResult,
    TERMINAL_STATUSES,
)
from app.platform_billing.models.provider import PlatformProviderCustomer
from app.platform_billing.providers.fake import DeterministicFakeProvider
from app.platform_billing.repositories.catalog import PlatformCatalogReadRepository
from app.platform_billing.repositories.provider_operations import PlatformProviderOperationRepository
from app.platform_billing.services.provider_operations import PlatformProviderOperationService
from app.platform_billing.services.query_service import PlatformBillingQueryService


class CheckoutConflictError(Exception):
    pass


class CheckoutPrerequisiteError(Exception):
    pass


class CheckoutPlanNotFoundError(Exception):
    pass


class PlatformCheckoutService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        provider_factory: Callable[[], DeterministicFakeProvider] = DeterministicFakeProvider,
        session_factory=AsyncSessionLocal,
    ):
        self._db = db
        self._provider_factory = provider_factory
        self._session_factory = session_factory

    async def create_checkout_session(
        self,
        request: CreateCheckoutSessionRequest,
        organization_id: uuid.UUID,
        idempotency_key: str,
    ) -> CreateCheckoutSessionResponse:
        query_service = PlatformBillingQueryService(self._db)
        subscription_detail = await query_service.get_current_subscription(organization_id)
        if subscription_detail is not None:
            raise CheckoutConflictError("versioned_flow_required")

        catalog = PlatformCatalogReadRepository(self._db)
        plans = await catalog.list_published_plan_versions()

        target_plan = None
        for plan in plans:
            if request.plan_id is not None and plan.id == request.plan_id:
                target_plan = plan
                break
            if request.plan_code is not None and plan.code == request.plan_code:
                target_plan = plan
                break

        if target_plan is None:
            raise CheckoutPlanNotFoundError("plan_not_found")

        now = datetime.now(timezone.utc)
        matching_prices = []
        for price in target_plan.prices:
            if price.status != "active" or price.published_at is None:
                continue
            if price.valid_from > now or (price.valid_until is not None and price.valid_until <= now):
                continue
            if request.billing_interval is not None:
                if price.billing_interval == request.billing_interval:
                    matching_prices.append(price)
            else:
                matching_prices.append(price)

        if len(matching_prices) == 0:
            raise CheckoutPlanNotFoundError("price_not_found")
        if len(matching_prices) > 1:
            raise CheckoutPrerequisiteError("multiple_prices_found")

        target_price = matching_prices[0]

        customer_result = await self._db.execute(
            select(PlatformProviderCustomer).where(
                PlatformProviderCustomer.organization_id == organization_id,
                PlatformProviderCustomer.provider_code == "fake",
                PlatformProviderCustomer.status == "active",
            )
        )
        customer = customer_result.scalar_one_or_none()
        if customer is None:
            raise CheckoutPrerequisiteError("provider_customer_missing")

        op_request = ProviderOperationRequest(
            organization_id=organization_id,
            provider_code="fake",
            operation_type="create_checkout",
            idempotency_key=idempotency_key,
            amount_minor=target_price.money.amount_minor,
            currency_code=target_price.money.currency_code,
            plan_version_id=target_plan.id,
            price_id=target_price.id,
            provider_customer_ref=customer.external_customer_ref,
        )

        provider_service = PlatformProviderOperationService(
            provider=self._provider_factory(),
            session_factory=self._session_factory,
        )
        try:
            result = await provider_service.execute(op_request)
            result = await self._wait_for_checkout_result(
                organization_id=organization_id,
                result=result,
            )
        except (IdempotencyConflict, ProviderOperationConflict) as e:
            raise CheckoutConflictError("idempotency_conflict") from e

        return CreateCheckoutSessionResponse(
            operation_id=result.operation_id,
            operation_status=result.status,
            checkout_session_reference=result.result_reference,
            fake_checkout_token=f"fake_token_{result.operation_id}" if result.status == "succeeded" else None,
            expires_at=None,
            confirmation_state="not_started",
            replayed=not result.provider_called,
            browser_authoritative=False,
        )


    async def _wait_for_checkout_result(
        self,
        *,
        organization_id: uuid.UUID,
        result: ProviderOperationResult,
    ) -> ProviderOperationResult:
        if result.provider_called or result.status in TERMINAL_STATUSES:
            return result

        for _ in range(50):
            await asyncio.sleep(0.01)
            async with self._session_factory() as session:
                repository = PlatformProviderOperationRepository(session)
                await repository.set_tenant_context(organization_id)
                snapshot = await repository.get_by_id(result.operation_id)
            if snapshot is not None and snapshot.status in TERMINAL_STATUSES:
                return ProviderOperationResult(
                    operation_id=snapshot.id,
                    status=snapshot.status,
                    external_operation_ref=snapshot.external_operation_ref,
                    error_classification=snapshot.error_classification,
                    result_evidence_sha256=snapshot.result_evidence_sha256,
                    result_reference=snapshot.result_reference,
                    provider_called=False,
                )
        return result

    async def get_checkout_operation(
        self,
        operation_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> GetCheckoutOperationResponse | None:
        repo = PlatformProviderOperationRepository(self._db)
        snapshot = await repo.get_by_id(operation_id)
        if snapshot is None or snapshot.organization_id != organization_id:
            return None

        if snapshot.operation_type != "create_checkout":
            return None

        return GetCheckoutOperationResponse(
            operation_id=snapshot.id,
            operation_status=snapshot.status,
            checkout_session_reference=snapshot.result_reference,
            expires_at=None,
            error_code=snapshot.error_classification,
            browser_authoritative=False,
        )
