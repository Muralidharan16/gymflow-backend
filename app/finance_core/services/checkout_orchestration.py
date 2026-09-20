from __future__ import annotations

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
    ProviderCheckoutIntentRequest,
)
from app.finance_core.models.foundation import FinanceInvoice, FinancePayment
from app.finance_core.services.checkout_intents import FinanceCheckoutIntentService
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine


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

    async def create_checkout_session(self, command: CreateCheckoutSessionCommand) -> SafeCheckoutSessionResult:
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

        provider_order_id = intent.provider_order_ref
        if not provider_order_id or provider_order_id.startswith("intent_"):
            provider_response = await self._provider_adapter.create_checkout_intent(
                ProviderCheckoutIntentRequest(
                    invoice_id=invoice.id,
                    amount=amount,
                    currency_code=invoice.currency_code,
                    idempotency_key=f"{command.idempotency_key}:razorpay_order",
                )
            )
            provider_order_id = provider_response.provider_order_ref
            await self._store_provider_order_id(intent.intent_id, provider_order_id)

        if not provider_order_id:
            raise ValueError("Checkout provider adapter did not return an order id")

        return SafeCheckoutSessionResult(
            finance_invoice_id=invoice.id,
            finance_checkout_intent_id=intent.intent_id,
            provider_order_id=provider_order_id,
            checkout_fields=self._provider_adapter.build_checkout_fields(
                provider_order_ref=provider_order_id
            ),
            display_amount=amount,
            display_currency=invoice.currency_code,
            replayed=draft.replayed or issued.replayed or intent.replayed,
        )

    async def _get_invoice(self, invoice_id):
        result = await self._session.execute(select(FinanceInvoice).where(FinanceInvoice.id == invoice_id).with_for_update())
        invoice = result.scalar_one()
        if invoice.status != "issued":
            raise ValueError("Checkout orchestration requires an issued invoice")
        return invoice

    async def _store_provider_order_id(self, intent_id, provider_order_id: str | None) -> None:
        if provider_order_id is None:
            return
        result = await self._session.execute(select(FinancePayment).where(FinancePayment.id == intent_id).with_for_update())
        payment = result.scalar_one()
        payment.provider_order_ref = provider_order_id
        await self._session.flush()
