from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import FinanceInvoiceNotFoundError, FinanceInvoiceStateError, canonical_hash, money
from app.finance_core.domain.payment_ledger import (
    CAPTURED_PAYMENT_STATUSES,
    AllocatePaymentCommand,
    ApplyPaymentToInvoiceCommand,
    FinancePaymentConflictError,
    FinancePaymentNotFoundError,
    FinancePaymentStateError,
    LedgerEntryResult,
    LedgerLineInput,
    PaymentAllocationResult,
    PaymentEventResult,
    PaymentResult,
    PaymentSettlementResult,
    PostLedgerEntryCommand,
    ReconcilePaymentSettlementCommand,
    RecordPaymentCommand,
    RecordPaymentEventCommand,
    validate_ledger_lines,
    validate_money_amount,
)
from app.finance_core.repositories.payments import FinancePaymentRepository


ACCOUNT_AR = "AR"
ACCOUNT_BANK = "BANK"
ACCOUNT_CLEARING = "PAYMENT_CLEARING"
ACCOUNT_GATEWAY_FEES = "PG_FEES"
ACCOUNT_REVENUE = "SAAS_REVENUE"
ACCOUNT_CGST = "CGST_PAYABLE"
ACCOUNT_SGST = "SGST_PAYABLE"
ACCOUNT_IGST = "IGST_PAYABLE"
SETTLEMENT_SOURCE_NAMESPACE = uuid.UUID("3ceded8b-66e0-4283-82d7-69ef11a67fb0")


class FinancePaymentLedgerService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = FinancePaymentRepository(session)

    async def record_payment(self, command: RecordPaymentCommand) -> PaymentResult:
        amount = validate_money_amount(command.amount, "Payment amount")
        payload = _record_payment_payload(command, amount)
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=command.organization_id,
            scope="finance.payment.record",
            idempotency_key=command.idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            payment = await self._repo.get_payment(uuid.UUID(idem.response_ref), for_update=True)
            if payment is None:
                raise FinancePaymentNotFoundError("Idempotent payment response could not be found")
            return await self._payment_result(payment, replayed=True)
        if not created:
            raise FinancePaymentConflictError("Payment record is already processing for this idempotency key")

        if command.provider_payment_ref is not None:
            existing = await self._repo.get_payment_by_provider_ref(
                provider_code=command.provider_code,
                provider_payment_ref=command.provider_payment_ref,
                for_update=True,
            )
            if existing is not None:
                raise FinancePaymentConflictError("Provider payment reference already exists")

        payment = await self._repo.create_payment(
            organization_id=command.organization_id,
            legal_entity_id=command.legal_entity_id,
            gst_registration_id=command.gst_registration_id,
            division_id=command.division_id,
            brand_id=command.brand_id,
            idempotency_key_id=idem.id,
            provider_code=command.provider_code,
            provider_payment_ref=command.provider_payment_ref,
            provider_order_ref=command.provider_order_ref,
            provider_signature_hash=command.provider_signature_hash,
            amount=amount,
            currency_code=command.currency_code.upper(),
            status=command.status,
            raw_status=command.raw_status,
        )
        await self._emit_outbox(
            aggregate_type="payment",
            aggregate_id=payment.id,
            event_type="finance.payment.recorded",
            idempotency_key=command.idempotency_key,
            payload={
                "payment_id": str(payment.id),
                "status": payment.status,
                "amount": str(payment.amount),
                "currency_code": payment.currency_code,
            },
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(payment.id))
        return await self._payment_result(payment)

    async def record_payment_event(self, command: RecordPaymentEventCommand) -> PaymentEventResult:
        payload = asdict(command)
        payload["payment_id"] = str(command.payment_id) if command.payment_id else None
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=None,
            scope="finance.payment.event.record",
            idempotency_key=command.idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            return PaymentEventResult(payment_event_id=uuid.UUID(idem.response_ref), replayed=True)
        if not created:
            raise FinancePaymentConflictError("Payment event record is already processing for this idempotency key")

        existing = await self._repo.get_payment_event_by_provider_id(
            provider_code=command.provider_code,
            provider_event_id=command.provider_event_id,
            for_update=True,
        )
        if existing is not None:
            raise FinancePaymentConflictError("Provider payment event already exists")

        event = await self._repo.create_payment_event(
            payment_id=command.payment_id,
            provider_code=command.provider_code,
            provider_event_id=command.provider_event_id,
            event_type=command.event_type,
            event_payload_sha256=command.event_payload_sha256,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(event.id))
        return PaymentEventResult(payment_event_id=event.id)

    async def allocate_payment_to_invoice(self, command: AllocatePaymentCommand) -> PaymentAllocationResult:
        return await self._allocate_payment_to_invoice(
            command=command,
            idempotency_scope="finance.payment.allocate",
            payment_event_type="finance.payment.allocated",
        )

    async def apply_payment_to_invoice(self, command: ApplyPaymentToInvoiceCommand) -> PaymentAllocationResult:
        return await self._allocate_payment_to_invoice(
            command=command,
            idempotency_scope="finance.payment.apply",
            payment_event_type="finance.payment.applied",
        )

    async def reconcile_payment_settlement(self, command: ReconcilePaymentSettlementCommand) -> PaymentSettlementResult:
        settlement_ref = command.settlement_ref.strip()
        idempotency_key = command.idempotency_key.strip()
        if not settlement_ref:
            raise FinancePaymentStateError("Settlement reference is required")
        if not idempotency_key:
            raise FinancePaymentStateError("Settlement reconciliation requires an idempotency key")

        settlement_amount = validate_money_amount(command.settlement_amount, "Settlement amount")
        gateway_fee_amount = money(command.gateway_fee_amount)
        if gateway_fee_amount < 0:
            raise FinancePaymentStateError("Gateway fee cannot be negative")
        if gateway_fee_amount > settlement_amount:
            raise FinancePaymentStateError("Gateway fee cannot exceed settlement amount")

        payment = await self._repo.get_payment(command.payment_id, for_update=True)
        if payment is None:
            raise FinancePaymentNotFoundError("Payment was not found")
        if payment.status not in CAPTURED_PAYMENT_STATUSES:
            raise FinancePaymentStateError("Only captured or settled payments can be reconciled")

        source_id = _settlement_source_id(settlement_ref)
        payload = _settlement_payload(
            payment_id=payment.id,
            settlement_ref=settlement_ref,
            settlement_amount=settlement_amount,
            gateway_fee_amount=gateway_fee_amount,
        )
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=payment.organization_id,
            scope="finance.payment.reconcile_settlement",
            idempotency_key=idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            return PaymentSettlementResult(
                payment_id=payment.id,
                settlement_ref=settlement_ref,
                ledger_entry_id=uuid.UUID(idem.response_ref),
                settlement_amount=settlement_amount,
                gateway_fee_amount=gateway_fee_amount,
                replayed=True,
            )
        if not created:
            raise FinancePaymentConflictError("Payment settlement reconciliation is already processing for this idempotency key")

        existing = await self._repo.get_ledger_entry_by_source(
            source_type="settlement",
            source_id=source_id,
            for_update=True,
        )
        if existing is not None:
            raise FinancePaymentConflictError("Settlement reference already exists")

        allocated_total = money(await self._repo.allocated_payment_total(payment.id))
        if allocated_total <= 0:
            raise FinancePaymentStateError("Only applied payments can be reconciled")
        reconciled_total = money(await self._repo.reconciled_payment_total(payment.id))
        unreconciled_amount = money(allocated_total - reconciled_total)
        if settlement_amount > unreconciled_amount:
            raise FinancePaymentStateError("Settlement amount cannot exceed unreconciled clearing amount")

        net_bank_amount = money(settlement_amount - gateway_fee_amount)
        lines: list[LedgerLineInput] = []
        if net_bank_amount > 0:
            lines.append(LedgerLineInput(account_code=ACCOUNT_BANK, debit_amount=net_bank_amount, memo="Settlement to bank"))
        if gateway_fee_amount > 0:
            lines.append(LedgerLineInput(account_code=ACCOUNT_GATEWAY_FEES, debit_amount=gateway_fee_amount, memo="Gateway fee"))
        lines.append(LedgerLineInput(account_code=ACCOUNT_CLEARING, credit_amount=settlement_amount, memo="Payment clearing reconciled"))

        ledger = await self.post_ledger_entry(
            PostLedgerEntryCommand(
                legal_entity_id=payment.legal_entity_id,
                division_id=payment.division_id,
                brand_id=payment.brand_id,
                entry_type="settlement",
                source_type="settlement",
                source_id=source_id,
                idempotency_key=f"{idempotency_key}:ledger",
                lines=tuple(lines),
            )
        )
        await self._emit_outbox(
            aggregate_type="payment",
            aggregate_id=payment.id,
            event_type="finance.payment.reconciled",
            idempotency_key=idempotency_key,
            payload=payload | {"ledger_entry_id": str(ledger.ledger_entry_id)},
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._emit_outbox(
            aggregate_type="settlement",
            aggregate_id=source_id,
            event_type="finance.settlement.reconciled",
            idempotency_key=idempotency_key,
            payload=payload | {"ledger_entry_id": str(ledger.ledger_entry_id)},
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._emit_outbox(
            aggregate_type="ledger_entry",
            aggregate_id=ledger.ledger_entry_id,
            event_type="finance.ledger.entry.posted",
            idempotency_key=f"{idempotency_key}:ledger",
            payload={"ledger_entry_id": str(ledger.ledger_entry_id), "source_type": "settlement"},
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(ledger.ledger_entry_id))
        await self._session.flush()
        return PaymentSettlementResult(
            payment_id=payment.id,
            settlement_ref=settlement_ref,
            ledger_entry_id=ledger.ledger_entry_id,
            settlement_amount=settlement_amount,
            gateway_fee_amount=gateway_fee_amount,
        )

    async def _allocate_payment_to_invoice(
        self,
        *,
        command: AllocatePaymentCommand | ApplyPaymentToInvoiceCommand,
        idempotency_scope: str,
        payment_event_type: str,
    ) -> PaymentAllocationResult:
        amount = validate_money_amount(command.amount, "Allocation amount")
        payment = await self._repo.get_payment(command.payment_id, for_update=True)
        if payment is None:
            raise FinancePaymentNotFoundError("Payment was not found")
        invoice = await self._repo.get_invoice(command.invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")

        payload = {
            "payment_id": str(command.payment_id),
            "invoice_id": str(command.invoice_id),
            "amount": str(amount),
        }
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=payment.organization_id,
            scope=idempotency_scope,
            idempotency_key=command.idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            allocation = await self._repo.get_allocation(uuid.UUID(idem.response_ref), for_update=True)
            if allocation is None:
                raise FinancePaymentNotFoundError("Idempotent allocation response could not be found")
            refreshed_invoice = await self._repo.get_invoice(allocation.invoice_id, for_update=True)
            if refreshed_invoice is None:
                raise FinanceInvoiceNotFoundError("Invoice was not found")
            return PaymentAllocationResult(
                allocation_id=allocation.id,
                payment_id=allocation.payment_id,
                invoice_id=allocation.invoice_id,
                invoice_status=refreshed_invoice.status,
                allocated_amount=allocation.allocated_amount,
                replayed=True,
            )
        if not created:
            raise FinancePaymentConflictError("Payment allocation is already processing for this idempotency key")

        self._validate_allocation_state(payment_status=payment.status, invoice_status=invoice.status)
        existing = await self._repo.get_allocation_for_payment_invoice(
            payment_id=payment.id,
            invoice_id=invoice.id,
            for_update=True,
        )
        if existing is not None:
            raise FinancePaymentConflictError("Payment is already allocated to this invoice")

        payment_available = money(payment.amount - await self._repo.allocated_payment_total(payment.id))
        invoice_outstanding = money(invoice.grand_total_amount - await self._repo.allocated_invoice_total(invoice.id))
        if amount > payment_available:
            raise FinancePaymentStateError("Allocation cannot exceed available payment balance")
        if amount > invoice_outstanding:
            raise FinancePaymentStateError("Allocation cannot exceed invoice outstanding amount")

        allocation = await self._repo.create_allocation(payment_id=payment.id, invoice_id=invoice.id, amount=amount)
        new_invoice_outstanding = money(invoice_outstanding - amount)
        invoice.status = "paid" if new_invoice_outstanding == 0 else "partially_paid"

        ledger = await self.post_payment_captured_entry(allocation_id=allocation.id, idempotency_key=f"{command.idempotency_key}:ledger")
        await self._emit_outbox(
            aggregate_type="payment_allocation",
            aggregate_id=allocation.id,
            event_type=payment_event_type,
            idempotency_key=command.idempotency_key,
            payload={
                "allocation_id": str(allocation.id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "allocated_amount": str(allocation.allocated_amount),
            },
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._emit_outbox(
            aggregate_type="invoice",
            aggregate_id=invoice.id,
            event_type="finance.invoice.paid" if invoice.status == "paid" else "finance.invoice.partially_paid",
            idempotency_key=command.idempotency_key,
            payload={"invoice_id": str(invoice.id), "status": invoice.status},
            organization_id=invoice.organization_id,
            legal_entity_id=invoice.legal_entity_id,
            division_id=invoice.division_id,
            brand_id=invoice.brand_id,
        )
        await self._emit_outbox(
            aggregate_type="ledger_entry",
            aggregate_id=ledger.ledger_entry_id,
            event_type="finance.ledger.entry.posted",
            idempotency_key=f"{command.idempotency_key}:ledger",
            payload={"ledger_entry_id": str(ledger.ledger_entry_id), "source_type": "payment_allocation"},
            organization_id=payment.organization_id,
            legal_entity_id=payment.legal_entity_id,
            division_id=payment.division_id,
            brand_id=payment.brand_id,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(allocation.id))
        await self._session.flush()
        return PaymentAllocationResult(
            allocation_id=allocation.id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            invoice_status=invoice.status,
            allocated_amount=allocation.allocated_amount,
        )

    async def post_invoice_issued_entry(self, *, invoice_id: uuid.UUID, idempotency_key: str) -> LedgerEntryResult:
        invoice = await self._repo.get_invoice(invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")
        if invoice.status not in {"issued", "partially_paid", "paid"}:
            raise FinanceInvoiceStateError("Only issued invoices can be posted to ledger")

        taxes = await self._repo.invoice_tax_component_totals(invoice.id)
        lines = [
            LedgerLineInput(account_code=ACCOUNT_AR, debit_amount=invoice.grand_total_amount, memo="Invoice receivable"),
            LedgerLineInput(account_code=ACCOUNT_REVENUE, credit_amount=invoice.taxable_amount, memo="SaaS revenue"),
        ]
        if taxes["cgst"] > 0:
            lines.append(LedgerLineInput(account_code=ACCOUNT_CGST, credit_amount=taxes["cgst"], memo="CGST payable"))
        if taxes["sgst"] > 0:
            lines.append(LedgerLineInput(account_code=ACCOUNT_SGST, credit_amount=taxes["sgst"], memo="SGST payable"))
        if taxes["igst"] > 0:
            lines.append(LedgerLineInput(account_code=ACCOUNT_IGST, credit_amount=taxes["igst"], memo="IGST payable"))
        result = await self.post_ledger_entry(
            PostLedgerEntryCommand(
                legal_entity_id=invoice.legal_entity_id,
                division_id=invoice.division_id,
                brand_id=invoice.brand_id,
                entry_type="invoice",
                source_type="invoice",
                source_id=invoice.id,
                idempotency_key=idempotency_key,
                lines=tuple(lines),
            )
        )
        if not result.replayed:
            await self._emit_outbox(
                aggregate_type="ledger_entry",
                aggregate_id=result.ledger_entry_id,
                event_type="finance.ledger.entry.posted",
                idempotency_key=idempotency_key,
                payload={"ledger_entry_id": str(result.ledger_entry_id), "source_type": "invoice"},
                organization_id=invoice.organization_id,
                legal_entity_id=invoice.legal_entity_id,
                division_id=invoice.division_id,
                brand_id=invoice.brand_id,
            )
        return result

    async def post_payment_captured_entry(self, *, allocation_id: uuid.UUID, idempotency_key: str) -> LedgerEntryResult:
        allocation = await self._repo.get_allocation(allocation_id, for_update=True)
        if allocation is None:
            raise FinancePaymentNotFoundError("Payment allocation was not found")
        payment = await self._repo.get_payment(allocation.payment_id, for_update=True)
        invoice = await self._repo.get_invoice(allocation.invoice_id, for_update=True)
        if payment is None:
            raise FinancePaymentNotFoundError("Payment was not found")
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")
        return await self.post_ledger_entry(
            PostLedgerEntryCommand(
                legal_entity_id=payment.legal_entity_id,
                division_id=payment.division_id or invoice.division_id,
                brand_id=payment.brand_id or invoice.brand_id,
                entry_type="payment",
                source_type="payment_allocation",
                source_id=allocation.id,
                idempotency_key=idempotency_key,
                lines=(
                    LedgerLineInput(account_code=ACCOUNT_CLEARING, debit_amount=allocation.allocated_amount, memo="Payment clearing"),
                    LedgerLineInput(account_code=ACCOUNT_AR, credit_amount=allocation.allocated_amount, memo="Receivable settled"),
                ),
            )
        )

    async def post_ledger_entry(self, command: PostLedgerEntryCommand) -> LedgerEntryResult:
        lines = validate_ledger_lines(command.lines)
        payload = _ledger_payload(command, lines)
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=None,
            scope="finance.ledger.post",
            idempotency_key=command.idempotency_key,
            request_hash=canonical_hash(payload),
        )
        if not created and idem.response_ref:
            return LedgerEntryResult(ledger_entry_id=uuid.UUID(idem.response_ref), status="posted", replayed=True)
        if not created:
            raise FinancePaymentConflictError("Ledger post is already processing for this idempotency key")

        existing = await self._repo.get_ledger_entry_by_source(
            source_type=command.source_type,
            source_id=command.source_id,
            for_update=True,
        )
        if existing is not None:
            await self._repo.complete_idempotency_key(idem, response_ref=str(existing.id))
            return LedgerEntryResult(ledger_entry_id=existing.id, status=existing.status, replayed=True)

        entry = await self._repo.create_ledger_entry(
            legal_entity_id=command.legal_entity_id,
            division_id=command.division_id,
            brand_id=command.brand_id,
            entry_type=command.entry_type,
            source_type=command.source_type,
            source_id=command.source_id,
            lines=lines,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(entry.id))
        return LedgerEntryResult(ledger_entry_id=entry.id, status=entry.status)

    async def _payment_result(self, payment, *, replayed: bool = False) -> PaymentResult:
        allocated = await self._repo.allocated_payment_total(payment.id)
        return PaymentResult(
            payment_id=payment.id,
            status=payment.status,
            amount=payment.amount,
            allocated_amount=allocated,
            replayed=replayed,
        )

    def _validate_allocation_state(self, *, payment_status: str, invoice_status: str) -> None:
        if payment_status not in CAPTURED_PAYMENT_STATUSES:
            raise FinancePaymentStateError("Only captured or settled payments can be allocated")
        if invoice_status not in {"issued", "partially_paid"}:
            raise FinanceInvoiceStateError("Only issued invoices with outstanding balance can receive payment allocation")

    async def _emit_outbox(
        self,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, object],
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID | None,
        division_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
    ) -> None:
        await self._repo.create_outbox_event(
            organization_id=organization_id,
            legal_entity_id=legal_entity_id,
            division_id=division_id,
            brand_id=brand_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            idempotency_key=idempotency_key,
            payload=payload,
        )


def _record_payment_payload(command: RecordPaymentCommand, amount: Decimal) -> dict[str, Any]:
    return {
        "organization_id": str(command.organization_id) if command.organization_id else None,
        "legal_entity_id": str(command.legal_entity_id),
        "gst_registration_id": str(command.gst_registration_id) if command.gst_registration_id else None,
        "division_id": str(command.division_id) if command.division_id else None,
        "brand_id": str(command.brand_id) if command.brand_id else None,
        "provider_code": command.provider_code,
        "provider_payment_ref": command.provider_payment_ref,
        "provider_order_ref": command.provider_order_ref,
        "provider_signature_hash": command.provider_signature_hash,
        "amount": str(amount),
        "currency_code": command.currency_code.upper(),
        "status": command.status,
        "raw_status": command.raw_status,
    }


def _ledger_payload(command: PostLedgerEntryCommand, lines: tuple[LedgerLineInput, ...]) -> dict[str, Any]:
    return {
        "legal_entity_id": str(command.legal_entity_id),
        "division_id": str(command.division_id) if command.division_id else None,
        "brand_id": str(command.brand_id) if command.brand_id else None,
        "entry_type": command.entry_type,
        "source_type": command.source_type,
        "source_id": str(command.source_id),
        "lines": [asdict(line) for line in lines],
    }


def _settlement_source_id(settlement_ref: str) -> uuid.UUID:
    return uuid.uuid5(SETTLEMENT_SOURCE_NAMESPACE, settlement_ref)


def _settlement_payload(
    *,
    payment_id: uuid.UUID,
    settlement_ref: str,
    settlement_amount: Decimal,
    gateway_fee_amount: Decimal,
) -> dict[str, str]:
    return {
        "payment_id": str(payment_id),
        "settlement_ref": settlement_ref,
        "settlement_amount": str(settlement_amount),
        "gateway_fee_amount": str(gateway_fee_amount),
    }
