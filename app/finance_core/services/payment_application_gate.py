from __future__ import annotations

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import (
    FinanceInvoiceNotFoundError,
    FinanceInvoiceStateError,
    canonical_hash,
    money,
)
from app.finance_core.domain.payment_application_gate import (
    AppliedPaymentResult,
    ApplyConfirmedPaymentCommand,
    FinancePaymentApplicationAuthorityError,
)
from app.finance_core.domain.payment_ledger import (
    FinancePaymentConflictError,
    FinancePaymentNotFoundError,
    FinancePaymentStateError,
)
from app.finance_core.repositories.payments import FinancePaymentRepository
from app.finance_core.services.operational_guards import FinanceOperationalGuardService
from app.finance_core.services.payment_ledger import FinancePaymentLedgerService


INTERNAL_PAYMENT_APPLICATION_ACTORS = {"finance_core", "system", "ops_admin"}


def _translate_payment_application_db_error(
    exc: DBAPIError,
):
    message = str(
        getattr(exc, "orig", None) or exc
    )

    if (
        "P4D finance payment application payment unavailable"
        in message
    ):
        return FinancePaymentNotFoundError(
            "Payment was not found"
        )

    if (
        "P4D finance payment application invoice unavailable"
        in message
    ):
        return FinanceInvoiceNotFoundError(
            "Invoice was not found"
        )

    if (
        "P4D finance payment application invoice state invalid"
        in message
    ):
        return FinanceInvoiceStateError(
            "Only issued invoices with outstanding balance "
            "can receive payment allocation"
        )

    if (
        "P4D finance idempotency request conflict"
        in message
        or "P4D finance payment application already processing"
        in message
        or "P4D finance payment application already allocated"
        in message
        or "P4D finance payment application replay unavailable"
        in message
        or "P4D finance payment application ledger already processing"
        in message
        or "P4D finance payment application ledger replay unavailable"
        in message
    ):
        return FinancePaymentConflictError(
            "Payment application conflicts with existing "
            "Finance state"
        )

    if (
        "P4D finance payment application tenant"
        in message
        or "P4D finance payment application requires app_runtime"
        in message
    ):
        return FinancePaymentApplicationAuthorityError(
            "Payment application tenant authority is invalid"
        )

    if (
        "P4D finance payment application request invalid"
        in message
        or "P4D finance payment application amount invalid"
        in message
        or "P4D finance payment application request hash invalid"
        in message
        or "P4D finance payment application payment state invalid"
        in message
        or "P4D finance payment application currency invalid"
        in message
        or "P4D finance payment application amount exceeds authority"
        in message
        or "P4D finance payment application relationship invalid"
        in message
        or "P4D finance payment application exceeds payment balance"
        in message
        or "P4D finance payment application exceeds invoice balance"
        in message
        or "P4D finance payment application clearing account unavailable"
        in message
        or "P4D finance payment application AR account unavailable"
        in message
    ):
        return FinancePaymentStateError(
            "Payment application state is invalid"
        )

    return None


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

    async def apply_confirmed_payment(
        self,
        command: ApplyConfirmedPaymentCommand,
    ) -> AppliedPaymentResult:
        self._validate_authority(command)
        self._guard_service.require_safe_preflight()

        amount = money(command.amount)

        request_hash = canonical_hash(
            {
                "payment_id": str(command.payment_id),
                "invoice_id": str(command.invoice_id),
                "amount": str(amount),
            }
        )

        try:
            async with self._session.begin_nested():
                row = (
                    await self._repo
                    .apply_confirmed_payment_capability(
                        payment_id=command.payment_id,
                        invoice_id=command.invoice_id,
                        amount=amount,
                        currency_code=(
                            command.currency_code.upper()
                        ),
                        idempotency_key=(
                            command.idempotency_key
                        ),
                        request_hash=request_hash,
                    )
                )
        except DBAPIError as exc:
            translated = (
                _translate_payment_application_db_error(exc)
            )
            if translated is None:
                raise
            raise translated from exc

        return AppliedPaymentResult(
            allocation_id=row["allocation_id"],
            payment_id=row["payment_id"],
            invoice_id=row["invoice_id"],
            invoice_status=row["invoice_status"],
            allocated_amount=row["allocated_amount"],
            replayed=row["replayed"],
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
