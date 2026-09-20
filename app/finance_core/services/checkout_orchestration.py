from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.checkout_orchestration import (
    CheckoutPlanResolver,
    CreateCheckoutSessionCommand,
    SafeCheckoutSessionResult,
)
from app.finance_core.domain.invoice_engine import CreateDraftInvoiceCommand, IssueInvoiceCommand, money
from app.finance_core.domain.provider_boundary import (
    CheckoutIntentProvider,
    CreateCheckoutIntentCommand,
    FinanceProviderConfigError,
    FinanceProviderOperationError,
    ProviderCheckoutIntentRequest,
    ProviderCheckoutIntentResponse,
)
from app.finance_core.models.foundation import FinanceInvoice
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


@dataclass(frozen=True)
class PreparedCheckoutSession:
    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    amount: Decimal
    currency_code: str
    provider_request: ProviderCheckoutIntentRequest
    provider_operation: ProviderOperationReservation | None
    provider_order_id: str | None
    replayed: bool


class FinanceCheckoutOrchestrationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        plan_resolver: CheckoutPlanResolver,
        provider_adapter: CheckoutIntentProvider | None = None,
        razorpay_adapter: CheckoutIntentProvider | None = None,
    ):
        if provider_adapter is not None and razorpay_adapter is not None:
            raise FinanceProviderConfigError(
                "Checkout orchestration accepts one provider adapter only."
            )
        adapter = provider_adapter or razorpay_adapter
        if adapter is None:
            raise FinanceProviderConfigError(
                "Checkout orchestration requires a provider adapter."
            )
        if adapter.environment not in {"sandbox", "test"}:
            raise FinanceProviderConfigError(
                "PAY-7 checkout orchestration accepts sandbox/test adapters only."
            )
        self._session = session
        self._plan_resolver = plan_resolver
        self._provider_adapter = adapter
        self._invoice_engine = FinanceInvoiceEngine(session)
        self._checkout_intents = FinanceCheckoutIntentService(session)
        self._provider_operations = FinanceProviderOperationService(session)

    async def prepare_checkout_session(
        self,
        command: CreateCheckoutSessionCommand,
    ) -> PreparedCheckoutSession:
        plan = await self._plan_resolver.resolve_plan(command.selector)
        draft = await self._invoice_engine.create_draft_invoice(
            CreateDraftInvoiceCommand(
                organization_id=command.organization_id,
                legal_entity_id=plan.legal_entity_id,
                gst_registration_id=plan.gst_registration_id,
                division_id=plan.division_id,
                brand_id=plan.brand_id,
                billing_party_id=command.billing_party_id,
                currency_code=plan.currency_code,
                supply_date=plan.supply_date,
                line_items=plan.line_items,
                idempotency_key=f"{command.idempotency_key}:invoice:create",
            )
        )
        issued = await self._invoice_engine.issue_invoice(
            IssueInvoiceCommand(
                invoice_id=draft.invoice_id,
                idempotency_key=f"{command.idempotency_key}:invoice:issue",
            )
        )
        invoice = await self._get_invoice(issued.invoice_id)
        amount = money(invoice.grand_total_amount)
        intent = await self._checkout_intents.create_checkout_intent(
            CreateCheckoutIntentCommand(
                organization_id=command.organization_id,
                invoice_id=invoice.id,
                provider_code=self._provider_adapter.provider_code,
                amount=amount,
                currency_code=invoice.currency_code,
                idempotency_key=f"{command.idempotency_key}:checkout_intent",
            )
        )
        request = ProviderCheckoutIntentRequest(
            invoice_id=invoice.id,
            amount=amount,
            currency_code=invoice.currency_code,
            idempotency_key=f"{command.idempotency_key}:provider_order",
        )
        provider_order_id = intent.provider_order_ref
        operation: ProviderOperationReservation | None = None
        if not provider_order_id or provider_order_id.startswith("intent_"):
            # Internal intent_* references are local placeholders only. They
            # must never escape as provider checkout objects or suppress the
            # first real provider create attempt.
            provider_order_id = None
            request_hash = provider_checkout_request_hash(
                payment_id=intent.intent_id,
                provider_code=self._provider_adapter.provider_code,
                environment=self._provider_adapter.environment,
                request=request,
            )
            operation = await self._provider_operations.reserve_checkout(
                payment_id=intent.intent_id,
                provider_code=self._provider_adapter.provider_code,
                environment=self._provider_adapter.environment,
                idempotency_key=request.idempotency_key,
                request_hash_sha256=request_hash,
            )
            if operation.status == "succeeded":
                provider_order_id = operation.provider_object_id

        return PreparedCheckoutSession(
            finance_invoice_id=invoice.id,
            finance_checkout_intent_id=intent.intent_id,
            amount=amount,
            currency_code=invoice.currency_code,
            provider_request=request,
            provider_operation=operation,
            provider_order_id=provider_order_id,
            replayed=draft.replayed or issued.replayed or intent.replayed,
        )

    async def claim_provider_operation(
        self,
        prepared: PreparedCheckoutSession,
        *,
        lease_owner: uuid.UUID,
    ) -> ProviderOperationClaim:
        if prepared.provider_operation is None:
            raise FinanceProviderConfigError(
                "Checkout provider operation is already complete."
            )
        return await self._provider_operations.claim(
            operation_id=prepared.provider_operation.operation_id,
            lease_owner=lease_owner,
        )

    async def call_provider(
        self,
        prepared: PreparedCheckoutSession,
    ) -> ProviderCheckoutIntentResponse:
        return await self._provider_adapter.create_checkout_intent(
            prepared.provider_request
        )

    async def finish_provider_success(
        self,
        prepared: PreparedCheckoutSession,
        claim: ProviderOperationClaim,
        *,
        lease_owner: uuid.UUID,
        response: ProviderCheckoutIntentResponse,
    ) -> str:
        if response.provider_code != self._provider_adapter.provider_code:
            raise FinanceProviderOperationError(
                provider_code=self._provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_CODE_MISMATCH",
                failure_class="unknown",
                message="Provider response identity is inconsistent.",
            )
        if not response.provider_order_ref:
            raise FinanceProviderOperationError(
                provider_code=self._provider_adapter.provider_code,
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
        claim: ProviderOperationClaim,
        *,
        lease_owner: uuid.UUID,
        error: FinanceProviderOperationError,
    ) -> None:
        outcome = {
            "retryable": "failed_retryable",
            "final": "failed_final",
            "unknown": "unknown",
        }[error.failure_class]
        await self._provider_operations.finish(
            operation_id=claim.operation_id,
            lease_owner=lease_owner,
            lease_fence=claim.lease_fence,
            outcome=outcome,
            provider_object_id=None,
            error_code=safe_provider_error_code(error),
            evidence_sha256=None,
        )

    def operation_state_error(
        self,
        status_value: str,
    ) -> FinanceProviderOperationError:
        if status_value == "failed_final":
            return FinanceProviderOperationError(
                provider_code=self._provider_adapter.provider_code,
                operation="create_checkout",
                code="PROVIDER_OPERATION_FINAL",
                failure_class="final",
                message="Provider checkout operation is terminal.",
            )
        return FinanceProviderOperationError(
            provider_code=self._provider_adapter.provider_code,
            operation="create_checkout",
            code="PROVIDER_OPERATION_UNRESOLVED",
            failure_class="unknown",
            message="Provider checkout operation is unresolved.",
        )

    def build_result(
        self,
        prepared: PreparedCheckoutSession,
        *,
        provider_order_id: str,
    ) -> SafeCheckoutSessionResult:
        return SafeCheckoutSessionResult(
            finance_invoice_id=prepared.finance_invoice_id,
            finance_checkout_intent_id=prepared.finance_checkout_intent_id,
            provider_order_id=provider_order_id,
            checkout_fields=self._provider_adapter.build_checkout_fields(
                provider_order_ref=provider_order_id
            ),
            display_amount=prepared.amount,
            display_currency=prepared.currency_code,
            replayed=prepared.replayed,
        )

    async def create_checkout_session(
        self,
        command: CreateCheckoutSessionCommand,
    ) -> SafeCheckoutSessionResult:
        """Compatibility path.

        Production HTTP composition uses the staged methods above with explicit
        commits before and after provider I/O. This helper remains for inherited
        unit/integration callers and still uses the same durable state machine.
        """
        prepared = await self.prepare_checkout_session(command)
        if prepared.provider_order_id:
            return self.build_result(
                prepared,
                provider_order_id=prepared.provider_order_id,
            )
        if prepared.provider_operation is None:
            raise FinanceProviderConfigError(
                "Checkout provider operation is unavailable."
            )
        if prepared.provider_operation.status not in (
            "reserved",
            "failed_retryable",
        ):
            raise self.operation_state_error(
                prepared.provider_operation.status
            )

        lease_owner = uuid.uuid4()
        claim = await self.claim_provider_operation(
            prepared,
            lease_owner=lease_owner,
        )
        if not claim.claimed:
            if claim.status == "succeeded" and claim.provider_object_id:
                return self.build_result(
                    prepared,
                    provider_order_id=claim.provider_object_id,
                )
            raise self.operation_state_error(claim.status)

        try:
            response = await self.call_provider(prepared)
        except FinanceProviderOperationError as exc:
            await self.finish_provider_error(
                claim,
                lease_owner=lease_owner,
                error=exc,
            )
            raise
        provider_order_id = await self.finish_provider_success(
            prepared,
            claim,
            lease_owner=lease_owner,
            response=response,
        )
        return self.build_result(
            prepared,
            provider_order_id=provider_order_id,
        )

    async def _get_invoice(self, invoice_id):
        result = await self._session.execute(select(FinanceInvoice).where(FinanceInvoice.id == invoice_id).with_for_update())
        invoice = result.scalar_one()
        if invoice.status != "issued":
            raise ValueError("Checkout orchestration requires an issued invoice")
        return invoice
