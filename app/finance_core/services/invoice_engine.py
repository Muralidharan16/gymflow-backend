from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import (
    CreateDraftInvoiceCommand,
    FinanceInvoiceConflictError,
    FinanceInvoiceNotFoundError,
    FinanceInvoiceStateError,
    FinanceInvoiceValidationError,
    IssueInvoiceCommand,
    InvoiceLineInput,
    InvoiceResult,
    calculate_invoice_totals,
    canonical_hash,
    financial_year_for,
)
from app.finance_core.repositories.invoices import FinanceInvoiceRepository, invoice_result


class FinanceInvoiceEngine:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._repo = FinanceInvoiceRepository(session)

    async def create_draft_invoice(self, command: CreateDraftInvoiceCommand) -> InvoiceResult:
        payload = _create_payload(command)
        request_hash = canonical_hash(payload)
        master = await self._load_master_data(
            organization_id=command.organization_id,
            legal_entity_id=command.legal_entity_id,
            gst_registration_id=command.gst_registration_id,
            division_id=command.division_id,
            brand_id=command.brand_id,
            billing_party_id=command.billing_party_id,
        )
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=command.organization_id,
            scope="finance.invoice.create",
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
        )
        if not created and idem.response_ref:
            invoice = await self._repo.get_invoice(uuid.UUID(idem.response_ref))
            if invoice is None:
                raise FinanceInvoiceNotFoundError("Idempotent draft invoice response could not be found")
            return invoice_result(invoice, replayed=True)
        if not created:
            raise FinanceInvoiceConflictError("Invoice creation is already processing for this idempotency key")

        totals = calculate_invoice_totals(
            supplier_state_code=master["gst_registration"].state_code,
            buyer_place_of_supply_state_code=master["billing_party"].place_of_supply_state_code,
            line_items=command.line_items,
        )
        invoice = await self._repo.create_invoice(
            organization_id=command.organization_id,
            billing_party_id=command.billing_party_id,
            legal_entity_id=command.legal_entity_id,
            gst_registration_id=command.gst_registration_id,
            division_id=command.division_id,
            brand_id=command.brand_id,
            financial_year=financial_year_for(command.supply_date),
            currency_code=command.currency_code.upper(),
            seller_legal_name=master["legal_entity"].legal_name,
            seller_gstin=master["gst_registration"].gstin,
            seller_pan=master["legal_entity"].pan,
            seller_registered_address=_required_text(master["gst_registration"].registered_address, "seller registered address"),
            seller_state_code=master["gst_registration"].state_code,
            buyer_billing_name=master["billing_party"].billing_name,
            buyer_address=_required_text(master["billing_party"].billing_address, "buyer billing address"),
            buyer_gstin=master["billing_party"].gstin,
            buyer_pan=master["billing_party"].pan,
            buyer_place_of_supply_state_code=master["billing_party"].place_of_supply_state_code,
            buyer_gst_treatment=master["billing_party"].gst_treatment,
            totals=totals,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(invoice.id))
        return invoice_result(invoice)

    async def issue_invoice(self, command: IssueInvoiceCommand) -> InvoiceResult:
        payload = {"invoice_id": str(command.invoice_id)}
        request_hash = canonical_hash(payload)
        invoice = await self._repo.get_invoice(command.invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")

        master = await self._load_master_data(
            organization_id=invoice.organization_id,
            legal_entity_id=invoice.legal_entity_id,
            gst_registration_id=invoice.gst_registration_id,
            division_id=invoice.division_id,
            brand_id=invoice.brand_id,
            billing_party_id=invoice.billing_party_id,
        )
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=invoice.organization_id,
            scope="finance.invoice.issue",
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
        )
        if not created and idem.response_ref:
            replayed = await self._repo.get_invoice(uuid.UUID(idem.response_ref), for_update=True)
            if replayed is None:
                raise FinanceInvoiceNotFoundError("Idempotent issued invoice response could not be found")
            return invoice_result(replayed, replayed=True)
        if not created:
            raise FinanceInvoiceConflictError("Invoice issue is already processing for this idempotency key")

        if invoice.status != "draft":
            raise FinanceInvoiceStateError("Only draft invoices can be issued")

        official_number = await self._repo.allocate_official_invoice_number(
            invoice,
            division_code=master["division"].code,
        )
        brand_reference = await self._repo.allocate_brand_reference(
            invoice,
            brand_code=master["brand"].code,
        )
        await self._repo.create_tax_records(invoice.id)
        invoice.status = "issued"
        invoice.issued_at = datetime.now(timezone.utc)

        event_payload = {
            "invoice_id": str(invoice.id),
            "official_invoice_number": official_number,
            "brand_reference": brand_reference,
            "status": "issued",
        }
        await self._repo.create_outbox_event(
            invoice=invoice,
            idempotency_key=command.idempotency_key,
            payload=event_payload,
            payload_sha256=canonical_hash(event_payload),
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(invoice.id))
        await self._session.flush()
        return invoice_result(invoice)

    async def replace_draft_lines(
        self,
        *,
        invoice_id: uuid.UUID,
        line_items: tuple[InvoiceLineInput, ...],
    ) -> InvoiceResult:
        invoice = await self._repo.get_invoice(invoice_id, for_update=True)
        if invoice is None:
            raise FinanceInvoiceNotFoundError("Invoice was not found")
        if invoice.status != "draft":
            raise FinanceInvoiceStateError("Issued invoices are immutable through the invoice engine")

        totals = calculate_invoice_totals(
            supplier_state_code=invoice.seller_state_code,
            buyer_place_of_supply_state_code=invoice.buyer_place_of_supply_state_code,
            line_items=line_items,
        )
        invoice.gst_supply_type = totals.gst_supply_type
        invoice.subtotal_amount = totals.subtotal_amount
        invoice.discount_amount = totals.discount_amount
        invoice.taxable_amount = totals.taxable_amount
        invoice.total_tax_amount = totals.total_tax_amount
        invoice.grand_total_amount = totals.grand_total_amount
        await self._repo.replace_invoice_lines(invoice.id, totals)
        return invoice_result(invoice)

    async def _load_master_data(
        self,
        *,
        organization_id: uuid.UUID | None,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID,
        division_id: uuid.UUID,
        brand_id: uuid.UUID,
        billing_party_id: uuid.UUID,
    ) -> dict[str, Any]:
        if organization_id is None:
            raise FinanceInvoiceValidationError("Invoice organization is required for billing-party ownership")
        organization = await self._repo.get_organization(organization_id)
        if organization is None:
            raise FinanceInvoiceValidationError("Invoice organization was not found")
        if not organization.is_active:
            raise FinanceInvoiceValidationError("Invoice organization is not active")

        legal_entity = await self._repo.get_legal_entity(legal_entity_id)
        gst_registration = await self._repo.get_gst_registration(gst_registration_id)
        division = await self._repo.get_division(division_id)
        brand = await self._repo.get_brand(brand_id)
        billing_party = await self._repo.get_billing_party(billing_party_id)
        if not all([legal_entity, gst_registration, division, brand, billing_party]):
            raise FinanceInvoiceValidationError("Invoice master data is incomplete")
        if gst_registration.legal_entity_id != legal_entity.id:
            raise FinanceInvoiceValidationError("GST registration does not belong to legal entity")
        if division.legal_entity_id != legal_entity.id:
            raise FinanceInvoiceValidationError("Division does not belong to legal entity")
        if brand.legal_entity_id != legal_entity.id or brand.division_id != division.id:
            raise FinanceInvoiceValidationError("Brand does not belong to division/legal entity")
        if billing_party.organization_id is None:
            raise FinanceInvoiceValidationError("Billing party ownership is required")
        if billing_party.organization_id != organization_id:
            raise FinanceInvoiceValidationError("Billing party does not belong to invoice organization")
        if billing_party.status != "active":
            raise FinanceInvoiceValidationError("Billing party is not active")
        return {
            "legal_entity": legal_entity,
            "gst_registration": gst_registration,
            "division": division,
            "brand": brand,
            "billing_party": billing_party,
        }


def _create_payload(command: CreateDraftInvoiceCommand) -> dict[str, Any]:
    return {
        "organization_id": str(command.organization_id) if command.organization_id else None,
        "legal_entity_id": str(command.legal_entity_id),
        "gst_registration_id": str(command.gst_registration_id),
        "division_id": str(command.division_id),
        "brand_id": str(command.brand_id),
        "billing_party_id": str(command.billing_party_id),
        "currency_code": command.currency_code.upper(),
        "supply_date": command.supply_date.isoformat(),
        "line_items": [asdict(line) for line in command.line_items],
    }


def _required_text(value: str | None, label: str) -> str:
    if value is None or value.strip() == "":
        raise FinanceInvoiceValidationError(f"Missing {label}")
    return value
