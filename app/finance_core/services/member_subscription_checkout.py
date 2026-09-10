from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import CreateDraftInvoiceCommand, InvoiceLineInput, IssueInvoiceCommand, money
from app.finance_core.domain.provider_boundary import CreateCheckoutIntentCommand, ProviderCheckoutIntentRequest
from app.finance_core.services.checkout_intents import FinanceCheckoutIntentService
from app.finance_core.services.invoice_engine import FinanceInvoiceEngine
from app.finance_core.services.razorpay_sandbox import RazorpaySandboxAdapter


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

    async def prepare_local_checkout(
        self,
        *,
        organization_id: uuid.UUID,
        subscription_id: uuid.UUID,
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
                provider_code=RazorpaySandboxAdapter.provider_code,
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
        await self._session.flush()
        return MemberSubscriptionCheckoutPreparation(
            organization_id=organization_id,
            subscription_id=subscription_id,
            finance_invoice_id=invoice["id"],
            finance_checkout_intent_id=intent.intent_id,
            amount=amount,
            currency_code=invoice["currency_code"],
            provider_order_ref=intent.provider_order_ref,
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

    async def create_provider_order_after_commit(
        self,
        *,
        prepared: MemberSubscriptionCheckoutPreparation,
        razorpay_adapter: RazorpaySandboxAdapter,
    ) -> MemberSubscriptionCheckoutResult:
        provider_order_ref = prepared.provider_order_ref
        if not provider_order_ref or provider_order_ref.startswith("intent_"):
            provider_response = await razorpay_adapter.create_checkout_intent(
                ProviderCheckoutIntentRequest(
                    invoice_id=prepared.finance_invoice_id,
                    amount=prepared.amount,
                    currency_code=prepared.currency_code,
                    idempotency_key=f"member-subscription-checkout:{prepared.subscription_id}:razorpay_order",
                )
            )
            if provider_response.provider_order_ref is None:
                raise MemberSubscriptionCheckoutConfigurationError("Razorpay sandbox adapter did not return an order id")
            provider_order_ref = provider_response.provider_order_ref
            await self.attach_provider_order(
                subscription_id=prepared.subscription_id,
                checkout_intent_id=prepared.finance_checkout_intent_id,
                provider_order_ref=provider_order_ref,
            )

        return MemberSubscriptionCheckoutResult(
            finance_invoice_id=prepared.finance_invoice_id,
            finance_checkout_intent_id=prepared.finance_checkout_intent_id,
            checkout_fields=razorpay_adapter.checkout_fields(order_id=provider_order_ref).to_browser_payload(),
            display_amount=prepared.amount,
            display_currency=prepared.currency_code,
            replayed=prepared.replayed,
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
