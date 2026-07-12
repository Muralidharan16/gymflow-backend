from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import FinanceInvoiceNotFoundError, money
from app.finance_core.domain.payment_application_gate import (
    AppliedPaymentResult,
    ApplyConfirmedPaymentCommand,
    FinancePaymentApplicationAuthorityError,
)
from app.finance_core.domain.payment_ledger import (
    CAPTURED_PAYMENT_STATUSES,
    ApplyPaymentToInvoiceCommand,
    FinancePaymentNotFoundError,
    FinancePaymentStateError,
)
from app.finance_core.repositories.payments import FinancePaymentRepository
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService


INTERNAL_PAYMENT_APPLICATION_ACTORS = {"finance_core", "system", "ops_admin"}


class FinancePaymentApplicationGateService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        guard_service: FinanceOperationalGuardService | None = None,
        ledger_service: FinancePaymentLedgerService | None = None,
    ):
        self._session = session
        self._repo = FinancePaymentRepository(session)
        self._guard_service = guard_service or FinanceOperationalGuardService()
        self._ledger_service = ledger_service or FinancePaymentLedgerService(session)

    async def apply_confirmed_payment(self, command: ApplyConfirmedPaymentCommand) -> AppliedPaymentResult:
        self._validate_authority(command)
        self._guard_service.require_safe_preflight()

        amount = money(command.amount)
        payment = await self._repo.get_payment(command.payment_id, for_update=True)
        if payment is None:
            raise FinancePaymentNotFoundError("Payment was not found")
        invoice = await self._repo.get_invoice(command.invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")

        if payment.status not in CAPTURED_PAYMENT_STATUSES:
            raise FinancePaymentStateError("Only captured or settled payments can be applied by the internal gate")
        if command.currency_code.upper() != payment.currency_code or command.currency_code.upper() != invoice.currency_code:
            raise FinancePaymentStateError("Payment application currency must match server-side payment and invoice currency")
        if amount > money(payment.amount):
            raise FinancePaymentStateError("Payment application amount cannot exceed server-side payment amount")
        if amount > money(invoice.grand_total_amount):
            raise FinancePaymentStateError("Payment application amount cannot exceed server-side invoice amount")
        if not self._payment_safely_matches_invoice(payment, invoice):
            raise FinancePaymentStateError("Payment does not safely match the target invoice")

        result = await self._ledger_service.apply_payment_to_invoice(
            ApplyPaymentToInvoiceCommand(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=amount,
                idempotency_key=command.idempotency_key,
            )
        )
        return AppliedPaymentResult(
            allocation_id=result.allocation_id,
            payment_id=result.payment_id,
            invoice_id=result.invoice_id,
            invoice_status=result.invoice_status,
            allocated_amount=result.allocated_amount,
            replayed=result.replayed,
        )

    def _validate_authority(self, command: ApplyConfirmedPaymentCommand) -> None:
        if not command.idempotency_key.strip():
            raise FinancePaymentApplicationAuthorityError("Internal payment application requires an idempotency key")
        if command.internal_actor not in INTERNAL_PAYMENT_APPLICATION_ACTORS:
            raise FinancePaymentApplicationAuthorityError("Payment application requires an explicit internal actor")
        if not command.reason.strip():
            raise FinancePaymentApplicationAuthorityError("Payment application requires an internal reason")

    def _payment_safely_matches_invoice(self, payment, invoice) -> bool:
        return (
            payment.organization_id == invoice.organization_id
            and payment.legal_entity_id == invoice.legal_entity_id
            and payment.gst_registration_id == invoice.gst_registration_id
            and payment.division_id == invoice.division_id
            and payment.brand_id == invoice.brand_id
            and money(payment.amount) <= money(invoice.grand_total_amount)
        )
