from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import CreateDraftInvoiceCommand, InvoiceLineInput, IssueInvoiceCommand, money
from app.finance_core.domain.provider_boundary import (
    CheckoutIntentProvider,
    CreateCheckoutIntentCommand,
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.services.checkout_intents import FinanceCheckoutIntentService
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine
from app.finance_core.services.provider_operations import (
    FinanceProviderOperationService,
    ProviderOperationClaim,
    ProviderOperationReservation,
    provider_checkout_request_hash,
    provider_checkout_success_hash,
    safe_provider_error_code,
)


class MemberSubscriptionCheckoutConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class MemberSubscriptionCheckoutPreparation:
    organization_id: uuid.UUID
    subscription_id: uuid.UUID
    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    amount: Decimal
    currency_code: str
    provider_order_ref: str | None
    provider_request: ProviderCheckoutIntentRequest
    provider_operation: ProviderOperationReservation | None
    replayed: bool


@dataclass(frozen=True)
class MemberSubscriptionCheckoutResult:
    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    checkout_fields: dict[str, str]
    display_amount: Decimal
    display_currency: str
    replayed: bool


class SourceBoundMemberSubscriptionCheckoutService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._invoice_engine = FinanceInvoiceEngine(session)
        self._checkout_intents = FinanceCheckoutIntentService(session)
        self._provider_operations = FinanceProviderOperationService(session)

    async def prepare_local_checkout(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
        provider_code: str,
        provider_environment: str,
    ) -> MemberSubscriptionCheckoutPreparation:
        inputs = await self._resolve_inputs(subscription_id)
        if inputs["organization_id"] != organization_id:
            raise MemberSubscriptionCheckoutConfigurationError("P4D source checkout tenant mismatch")

        source_key = f"member-subscription-checkout:{subscription_id}"
        draft = await self._invoice_engine.create_draft_invoice(
            CreateDraftInvoiceCommand(
                organization_id=organization_id,
                legal_entity_id=inputs["legal_entity_id"],
                gst_registration_id=inputs["gst_registration_id"],
                division_id=inputs["division_id"],
                brand_id=inputs["brand_id"],
                billing_party_id=inputs["billing_party_id"],
                currency_code=inputs["currency_code"],
                supply_date=inputs["supply_date"],
                line_items=(
                    InvoiceLineInput(
                        description=inputs["line_description"],
                        quantity=Decimal("1.000"),
                        unit_price=money(inputs["unit_price"]),
                        hsn_sac=inputs["hsn_sac"],
                        gst_rate_basis_points=int(inputs["gst_rate_basis_points"]),
                        pricing_mode=inputs["pricing_mode"],
                        discount_amount=Decimal("0.00"),
                    ),
                ),
                idempotency_key=f"{source_key}:invoice:create",
            )
        )
        issued = await self._invoice_engine.issue_invoice(
            IssueInvoiceCommand(
                invoice_id=draft.invoice_id,
                idempotency_key=f"{source_key}:invoice:issue",
            )
        )
        invoice_row = await self._session.execute(
            text(
                """
                SELECT id, grand_total_amount, currency_code
                FROM finance.invoices
                WHERE id = :invoice_id
                  AND organization_id = :organization_id
                """
            ),
            {"invoice_id": issued.invoice_id, "organization_id": organization_id},
        )
        invoice = invoice_row.mappings().one()
        amount = money(invoice["grand_total_amount"])
        intent = await self._checkout_intents.create_checkout_intent(
            CreateCheckoutIntentCommand(
                organization_id=organization_id,
                invoice_id=invoice["id"],
                provider_code=provider_code,
                amount=amount,
                currency_code=invoice["currency_code"],
                idempotency_key=f"{source_key}:checkout_intent",
            )
        )
        await self._session.execute(
            text("SELECT * FROM app_secure.record_refund_obligation_binding(:invoice_id, :subscription_id)"),
            {"invoice_id": invoice["id"], "subscription_id": subscription_id},
        )
        await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.record_member_subscription_checkout_binding(
                    :subscription_id, :invoice_id, :checkout_intent_id
                )
                """
            ),
            {
                "subscription_id": subscription_id,
                "invoice_id": invoice["id"],
                "checkout_intent_id": intent.intent_id,
            },
        )
        # PAY-4 canonical authority is term-based. The legacy checkout binding
        # remains compatibility evidence only; this second capability derives
        # the immutable term/member/plan/amount/currency relationship.
        await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.record_member_subscription_finance_binding(
                    :subscription_id, :invoice_id
                )
                """
            ),
            {
                "subscription_id": subscription_id,
                "invoice_id": invoice["id"],
            },
        )
        request = ProviderCheckoutIntentRequest(
            invoice_id=invoice["id"],
            amount=amount,
            currency_code=invoice["currency_code"],
            idempotency_key=f"{source_key}:provider_order",
        )
        provider_order_ref = intent.provider_order_ref
        provider_operation: ProviderOperationReservation | None = None
        if not provider_order_ref or provider_order_ref.startswith("intent_"):
            provider_operation = await self._provider_operations.reserve_checkout(
                payment_id=intent.intent_id,
                provider_code=provider_code,
                environment=provider_environment,
                idempotency_key=request.idempotency_key,
                request_hash_sha256=provider_checkout_request_hash(
                    payment_id=intent.intent_id,
                    provider_code=provider_code,
                    environment=provider_environment,
                    request=request,
                ),
            )
            if provider_operation.status == "succeeded":
                provider_order_ref = provider_operation.provider_object_id

        await self._session.flush()
        return MemberSubscriptionCheckoutPreparation(
            organization_id=organization_id,
            subscription_id=subscription_id,
            finance_invoice_id=invoice["id"],
            finance_checkout_intent_id=intent.intent_id,
            amount=amount,
            currency_code=invoice["currency_code"],
            provider_order_ref=provider_order_ref,
            provider_request=request,
            provider_operation=provider_operation,
            replayed=draft.replayed or issued.replayed or intent.replayed,
        )

    async def attach_provider_order(
        self,
        *,
        subscription_id: uuid.UUID,
        checkout_intent_id: uuid.UUID,
        provider_order_ref: str,
    ) -> None:
        await self._session.execute(
            text(
                """
                SELECT *
                FROM app_secure.attach_member_subscription_checkout_provider_order(
                    :subscription_id, :checkout_intent_id, :provider_order_ref
                )
                """
            ),
            {
                "subscription_id": subscription_id,
                "checkout_intent_id": checkout_intent_id,
                "provider_order_ref": provider_order_ref,
            },
        )
        await self._session.flush()

    async def claim_provider_operation(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        lease_owner: uuid.UUID,
    ) -> ProviderOperationClaim:
        if prepared.provider_operation is None:
            raise MemberSubscriptionCheckoutConfigurationError(
                "PROVIDER_OPERATION_UNAVAILABLE"
            )
        return await self._provider_operations.claim(
            operation_id=prepared.provider_operation.operation_id,
            lease_owner=lease_owner,
        )

    async def call_provider(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        provider_adapter: CheckoutIntentProvider,
    ) -> ProviderCheckoutIntentResponse:
        return await provider_adapter.create_checkout_intent(
            prepared.provider_request
        )

    async def finish_provider_success(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        provider_adapter: CheckoutIntentProvider,
        claim: ProviderOperationClaim,
        lease_owner: uuid.UUID,
        response: ProviderCheckoutIntentResponse,
    ) -> str:
        if response.provider_code != provider_adapter.provider_code:
            raise FinanceProviderOperationError(
                provider_code=provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_CODE_MISMATCH",
                failure_class="unknown",
                message="Provider response identity is inconsistent.",
            )
        if not response.provider_order_ref:
            raise FinanceProviderOperationError(
                provider_code=provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_ORDER_REQUIRED",
                failure_class="unknown",
                message="Provider response did not contain an order reference.",
            )
        await self._provider_operations.finish(
            operation_id=claim.operation_id,
            lease_owner=lease_owner,
            lease_fence=claim.lease_fence,
            outcome="succeeded",
            provider_object_id=response.provider_order_ref,
            error_code=None,
            evidence_sha256=provider_checkout_success_hash(response),
        )
        return response.provider_order_ref

    async def finish_provider_error(
        self,
        *,
        claim: ProviderOperationClaim,
        lease_owner: uuid.UUID,
        error: FinanceProviderOperationError,
    ) -> None:
        await self._provider_operations.finish(
            operation_id=claim.operation_id,
            lease_owner=lease_owner,
            lease_fence=claim.lease_fence,
            outcome={
                "retryable": "failed_retryable",
                "final": "failed_final",
                "unknown": "unknown",
            }[error.failure_class],
            provider_object_id=None,
            error_code=safe_provider_error_code(error),
            evidence_sha256=None,
        )

    def build_result(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        provider_adapter: CheckoutIntentProvider,
        provider_order_ref: str,
    ) -> MemberSubscriptionCheckoutResult:
        return MemberSubscriptionCheckoutResult(
            finance_invoice_id=prepared.finance_invoice_id,
            finance_checkout_intent_id=prepared.finance_checkout_intent_id,
            checkout_fields=provider_adapter.build_checkout_fields(
                provider_order_ref=provider_order_ref
            ),
            display_amount=prepared.amount,
            display_currency=prepared.currency_code,
            replayed=prepared.replayed,
        )

    def operation_state_error(
        self,
        *,
        provider_code: str,
        status_value: str,
    ) -> FinanceProviderOperationError:
        if status_value == "failed_final":
            return FinanceProviderOperationError(
                provider_code=provider_code,
                operation="create_checkout",
                code="PROVIDER_OPERATION_FINAL",
                failure_class="final",
                message="Provider checkout operation is terminal.",
            )
        return FinanceProviderOperationError(
            provider_code=provider_code,
            operation="create_checkout",
            code="PROVIDER_OPERATION_UNRESOLVED",
            failure_class="unknown",
            message="Provider checkout operation is unresolved.",
        )

    async def create_provider_order_after_commit(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        provider_adapter: CheckoutIntentProvider | None = None,
        razorpay_adapter: CheckoutIntentProvider | None = None,
    ) -> MemberSubscriptionCheckoutResult:
        """Compatibility helper; route composition uses staged commit methods."""
        if provider_adapter is not None and razorpay_adapter is not None:
            raise FinanceProviderConfigError(
                "Member checkout accepts one provider adapter only."
            )
        adapter = provider_adapter or razorpay_adapter
        if adapter is None:
            raise FinanceProviderConfigError(
                "Member checkout requires a provider adapter."
            )

        if prepared.provider_order_ref:
            return self.build_result(
                prepared=prepared,
                provider_adapter=adapter,
                provider_order_ref=prepared.provider_order_ref,
            )
        if prepared.provider_operation is None:
            raise MemberSubscriptionCheckoutConfigurationError(
                "PROVIDER_OPERATION_UNAVAILABLE"
            )
        if prepared.provider_operation.status not in (
            "reserved",
            "failed_retryable",
        ):
            raise self.operation_state_error(
                provider_code=adapter.provider_code,
                status_value=prepared.provider_operation.status,
            )

        lease_owner = uuid.uuid4()
        claim = await self.claim_provider_operation(
            prepared=prepared,
            lease_owner=lease_owner,
        )
        if not claim.claimed:
            if claim.status == "succeeded" and claim.provider_object_id:
                return self.build_result(
                    prepared=prepared,
                    provider_adapter=adapter,
                    provider_order_ref=claim.provider_object_id,
                )
            raise self.operation_state_error(
                provider_code=adapter.provider_code,
                status_value=claim.status,
            )
        try:
            response = await self.call_provider(
                prepared=prepared,
                provider_adapter=adapter,
            )
        except FinanceProviderOperationError as exc:
            await self.finish_provider_error(
                claim=claim,
                lease_owner=lease_owner,
                error=exc,
            )
            raise
        provider_order_ref = await self.finish_provider_success(
            prepared=prepared,
            provider_adapter=adapter,
            claim=claim,
            lease_owner=lease_owner,
            response=response,
        )
        return self.build_result(
            prepared=prepared,
            provider_adapter=adapter,
            provider_order_ref=provider_order_ref,
        )

    async def _resolve_inputs(self, subscription_id: uuid.UUID) -> dict[str, object]:
        result = await self._session.execute(
            text("SELECT * FROM app_secure.resolve_member_subscription_checkout_inputs(:subscription_id)"),
            {"subscription_id": subscription_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise MemberSubscriptionCheckoutConfigurationError("CONFIGURATION_REQUIRED")
        return dict(row)
