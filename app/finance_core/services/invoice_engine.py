from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DBAPIError
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
            supply_date=command.supply_date,
        )
        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=command.organization_id,
            scope="finance.invoice.create",
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
        )
        if not created and idem.response_ref:
            replayed_result = await self._repo.resolve_invoice_result(uuid.UUID(idem.response_ref))
            if replayed_result is None:
                raise FinanceInvoiceNotFoundError("Idempotent draft invoice response could not be found")
            return InvoiceResult(
                invoice_id=replayed_result.invoice_id,
                status=replayed_result.status,
                official_invoice_number=replayed_result.official_invoice_number,
                brand_reference=replayed_result.brand_reference,
                replayed=True,
            )
        if not created:
            raise FinanceInvoiceConflictError("Invoice creation is already processing for this idempotency key")

        totals = calculate_invoice_totals(
            supplier_state_code=master.seller_state_code,
            buyer_place_of_supply_state_code=master.buyer_place_of_supply_state_code,
            line_items=command.line_items,
        )
        invoice = await self._repo.create_invoice(
            idempotency_key_id=idem.id,
            organization_id=command.organization_id,
            billing_party_id=command.billing_party_id,
            legal_entity_id=command.legal_entity_id,
            gst_registration_id=command.gst_registration_id,
            division_id=command.division_id,
            brand_id=command.brand_id,
            supply_date=command.supply_date,
            financial_year=financial_year_for(command.supply_date),
            currency_code=command.currency_code.upper(),
            seller_legal_name=master.seller_legal_name,
            seller_gstin=master.seller_gstin,
            seller_pan=master.seller_pan,
            seller_registered_address=_required_text(master.seller_registered_address, "seller registered address"),
            seller_state_code=master.seller_state_code,
            buyer_billing_name=master.buyer_billing_name,
            buyer_address=_required_text(master.buyer_address, "buyer billing address"),
            buyer_gstin=master.buyer_gstin,
            buyer_pan=master.buyer_pan,
            buyer_place_of_supply_state_code=master.buyer_place_of_supply_state_code,
            buyer_gst_treatment=master.buyer_gst_treatment,
            totals=totals,
        )
        await self._repo.complete_idempotency_key(idem, response_ref=str(invoice.id))
        return invoice_result(invoice)

    async def issue_invoice(
        self,
        command: IssueInvoiceCommand,
    ) -> InvoiceResult:
        payload = {"invoice_id": str(command.invoice_id)}
        request_hash = canonical_hash(payload)

        context = await self._repo.resolve_invoice_issue_context(
            command.invoice_id
        )

        if context is None:
            raise FinanceInvoiceNotFoundError(
                "Invoice was not found"
            )

        organization_id = context["organization_id"]

        if not isinstance(organization_id, uuid.UUID):
            raise FinanceInvoiceValidationError(
                "Invoice organization context is invalid"
            )

        idem, created = await self._repo.reserve_idempotency_key(
            organization_id=organization_id,
            scope="finance.invoice.issue",
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
        )

        if not created and idem.response_ref:
            try:
                response_invoice_id = uuid.UUID(
                    idem.response_ref
                )
            except (TypeError, ValueError) as exc:
                raise FinanceInvoiceConflictError(
                    "Invoice issue idempotency response is invalid"
                ) from exc

            if response_invoice_id != command.invoice_id:
                raise FinanceInvoiceConflictError(
                    "Invoice issue idempotency key belongs to "
                    "another invoice"
                )

            replayed = await self._repo.resolve_invoice_result(
                response_invoice_id
            )

            if replayed is None:
                raise FinanceInvoiceNotFoundError(
                    "Idempotent issued invoice response "
                    "could not be found"
                )

            return InvoiceResult(
                invoice_id=replayed.invoice_id,
                status=replayed.status,
                official_invoice_number=(
                    replayed.official_invoice_number
                ),
                brand_reference=replayed.brand_reference,
                replayed=True,
            )

        if not created:
            raise FinanceInvoiceConflictError(
                "Invoice issue is already processing for "
                "this idempotency key"
            )

        try:
            issued = await self._repo.issue_invoice_atomic(
                invoice_id=command.invoice_id,
                idempotency_key=command.idempotency_key,
            )
        except DBAPIError as exc:
            message = str(
                getattr(exc, "orig", None) or exc
            )
            if (
                'P4D finance invoice accounting master data unavailable'
                in message
            ):
                raise FinanceInvoiceValidationError(
                    'Invoice cannot be issued because persisted finance data failed validation'
                ) from exc
            raise

        await self._repo.complete_idempotency_key(
            idem,
            response_ref=str(issued.invoice_id),
        )

        return issued

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
        supply_date,
    ) -> Any:
        if organization_id is None:
            raise FinanceInvoiceValidationError("Invoice organization is required for billing-party ownership")
        organization = await self._repo.get_organization(organization_id)
        if organization is None:
            raise FinanceInvoiceValidationError("Invoice organization was not found")
        if not organization.is_active:
            raise FinanceInvoiceValidationError("Invoice organization is not active")

        try:
            master = await self._repo.resolve_invoice_accounting_master_data(
                organization_id=organization_id,
                legal_entity_id=legal_entity_id,
                gst_registration_id=gst_registration_id,
                division_id=division_id,
                brand_id=brand_id,
                billing_party_id=billing_party_id,
                supply_date=supply_date,
            )
        except DBAPIError as exc:
            message = str(
                getattr(exc, "orig", None) or exc
            )
            if (
                "P4D finance invoice accounting master data unavailable"
                in message
            ):
                raise FinanceInvoiceValidationError(
                    (
                        "Invoice accounting master data is "
                        "incomplete or unavailable"
                    )
                ) from exc
            raise
        if master is None:
            raise FinanceInvoiceValidationError("Invoice master data is incomplete")
        return master


def _invoice_supply_date(invoice) -> datetime.date:
    value = (invoice.metadata_json or {}).get("supply_date")
    if not isinstance(value, str):
        raise FinanceInvoiceValidationError("Invoice supply date is required")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise FinanceInvoiceValidationError("Invoice supply date is invalid") from exc


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
