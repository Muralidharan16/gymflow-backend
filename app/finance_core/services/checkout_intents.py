from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import FinanceInvoiceNotFoundError, canonical_hash, money
from app.finance_core.domain.provider_boundary import (
    CheckoutIntentResult,
    CreateCheckoutIntentCommand,
    FinanceCheckoutIntentConflictError,
    FinanceCheckoutIntentStateError,
)
from app.finance_core.repositories.checkout_intents import FinanceCheckoutIntentRepository


class FinanceCheckoutIntentService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = FinanceCheckoutIntentRepository(session)

    async def create_checkout_intent(self, command: CreateCheckoutIntentCommand) -> CheckoutIntentResult:
        amount = money(command.amount)
        payload = {
            "organization_id": str(command.organization_id) if command.organization_id else None,
            "invoice_id": str(command.invoice_id),
            "provider_code": command.provider_code,
            "amount": str(amount),
            "currency_code": command.currency_code.upper(),
        }
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=command.organization_id,
            scope="finance.checkout_intent.create",
            idempotency_key=command.idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            intent = await self._repo.get_intent(uuid.UUID(idem.response_ref), for_update=True)
            if intent is None:
                raise FinanceCheckoutIntentConflictError("Idempotent checkout intent response could not be found")
            return _intent_result(intent, invoice_id=command.invoice_id, replayed=True)
        if not created:
            raise FinanceCheckoutIntentConflictError("Checkout intent creation is already processing for this idempotency key")

        invoice = await self._repo.get_invoice(command.invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")
        if invoice.status == "draft":
            raise FinanceCheckoutIntentStateError("Draft invoices cannot create checkout intents")
        if invoice.status == "paid":
            raise FinanceCheckoutIntentStateError("Paid invoices cannot create checkout intents")
        if invoice.status != "issued":
            raise FinanceCheckoutIntentStateError("Checkout intents can only be created for issued invoices")
        if amount != money(invoice.grand_total_amount):
            raise FinanceCheckoutIntentStateError("Checkout intent amount must match issued invoice total")
        if command.currency_code.upper() != invoice.currency_code:
            raise FinanceCheckoutIntentStateError("Checkout intent currency must match invoice currency")

        provider_order_ref = f"intent_{uuid.uuid4().hex}"
        intent = await self._repo.create_intent(
            organization_id=invoice.organization_id,
            legal_entity_id=invoice.legal_entity_id,
            gst_registration_id=invoice.gst_registration_id,
            division_id=invoice.division_id,
            brand_id=invoice.brand_id,
            idempotency_key_id=idem.id,
            provider_code=command.provider_code,
            provider_order_ref=provider_order_ref,
            amount=amount,
            currency_code=invoice.currency_code,
        )
        event_payload = {
            "intent_id": str(intent.id),
            "invoice_id": str(invoice.id),
            "status": intent.status,
            "amount": str(intent.amount),
            "currency_code": intent.currency_code,
            "provider_code": intent.provider_code,
        }
        await self._repo.create_outbox_event(
            organization_id=invoice.organization_id,
            legal_entity_id=invoice.legal_entity_id,
            division_id=invoice.division_id,
            brand_id=invoice.brand_id,
            aggregate_id=intent.id,
            idempotency_key=command.idempotency_key,
            payload=event_payload,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(intent.id))
        await self._session.flush()
        return _intent_result(intent, invoice_id=invoice.id)


def _intent_result(intent, *, invoice_id: uuid.UUID, replayed: bool = False) -> CheckoutIntentResult:
    return CheckoutIntentResult(
        intent_id=intent.id,
        invoice_id=invoice_id,
        status=intent.status,
        amount=Decimal(intent.amount),
        currency_code=intent.currency_code,
        provider_code=intent.provider_code,
        provider_order_ref=intent.provider_order_ref,
        replayed=replayed,
    )
