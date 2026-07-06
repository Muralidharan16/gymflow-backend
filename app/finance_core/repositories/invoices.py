from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import (
    CalculatedInvoiceTotals,
    FinanceInvoiceConflictError,
    InvoiceResult,
    invoice_number,
)
from app.finance_core.models.foundation import (
    FinanceBillingParty,
    FinanceBrand,
    FinanceBrandRefSeries,
    FinanceDivision,
    FinanceGstRegistration,
    FinanceIdempotencyKey,
    FinanceInvoice,
    FinanceInvoiceLine,
    FinanceInvoiceSeries,
    FinanceLegalEntity,
    FinanceOutboxEvent,
    FinanceTaxRecord,
)


class FinanceInvoiceRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_invoice(self, invoice_id: uuid.UUID, *, for_update: bool = False) -> FinanceInvoice | None:
        statement = select(FinanceInvoice).where(FinanceInvoice.id == invoice_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_legal_entity(self, legal_entity_id: uuid.UUID) -> FinanceLegalEntity | None:
        result = await self._session.execute(select(FinanceLegalEntity).where(FinanceLegalEntity.id == legal_entity_id))
        return result.scalar_one_or_none()

    async def get_gst_registration(self, gst_registration_id: uuid.UUID) -> FinanceGstRegistration | None:
        result = await self._session.execute(select(FinanceGstRegistration).where(FinanceGstRegistration.id == gst_registration_id))
        return result.scalar_one_or_none()

    async def get_division(self, division_id: uuid.UUID) -> FinanceDivision | None:
        result = await self._session.execute(select(FinanceDivision).where(FinanceDivision.id == division_id))
        return result.scalar_one_or_none()

    async def get_brand(self, brand_id: uuid.UUID) -> FinanceBrand | None:
        result = await self._session.execute(select(FinanceBrand).where(FinanceBrand.id == brand_id))
        return result.scalar_one_or_none()

    async def get_billing_party(self, billing_party_id: uuid.UUID) -> FinanceBillingParty | None:
        result = await self._session.execute(select(FinanceBillingParty).where(FinanceBillingParty.id == billing_party_id))
        return result.scalar_one_or_none()

    async def reserve_idempotency_key(
        self,
        *,
        organization_id: uuid.UUID | None,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[FinanceIdempotencyKey, bool]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        statement = (
            insert(FinanceIdempotencyKey)
            .values(
                organization_id=organization_id,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash_sha256=request_hash,
                status="processing",
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_finance_idempotency_keys_scope_key")
            .returning(FinanceIdempotencyKey)
        )
        inserted = await self._session.execute(statement)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True

        existing_result = await self._session.execute(
            select(FinanceIdempotencyKey)
            .where(
                FinanceIdempotencyKey.scope == scope,
                FinanceIdempotencyKey.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        existing = existing_result.scalar_one()
        if existing.request_hash_sha256 != request_hash:
            raise FinanceInvoiceConflictError("Idempotency key already exists for a different invoice request")
        return existing, False

    async def complete_idempotency_key(self, key: FinanceIdempotencyKey, *, response_ref: str) -> None:
        key.status = "succeeded"
        key.response_ref = response_ref
        await self._session.flush()

    async def create_invoice(
        self,
        *,
        organization_id: uuid.UUID | None,
        billing_party_id: uuid.UUID,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID,
        division_id: uuid.UUID,
        brand_id: uuid.UUID,
        financial_year: str,
        currency_code: str,
        seller_legal_name: str,
        seller_gstin: str,
        seller_pan: str | None,
        seller_registered_address: str,
        seller_state_code: str,
        buyer_billing_name: str,
        buyer_address: str,
        buyer_gstin: str | None,
        buyer_pan: str | None,
        buyer_place_of_supply_state_code: str,
        buyer_gst_treatment: str,
        totals: CalculatedInvoiceTotals,
    ) -> FinanceInvoice:
        invoice = FinanceInvoice(
            organization_id=organization_id,
            billing_party_id=billing_party_id,
            legal_entity_id=legal_entity_id,
            gst_registration_id=gst_registration_id,
            division_id=division_id,
            brand_id=brand_id,
            financial_year=financial_year,
            status="draft",
            currency_code=currency_code,
            seller_legal_name=seller_legal_name,
            seller_gstin=seller_gstin,
            seller_pan=seller_pan,
            seller_registered_address=seller_registered_address,
            seller_state_code=seller_state_code,
            buyer_billing_name=buyer_billing_name,
            buyer_address=buyer_address,
            buyer_gstin=buyer_gstin,
            buyer_pan=buyer_pan,
            buyer_place_of_supply_state_code=buyer_place_of_supply_state_code,
            buyer_gst_treatment=buyer_gst_treatment,
            gst_supply_type=totals.gst_supply_type,
            subtotal_amount=totals.subtotal_amount,
            discount_amount=totals.discount_amount,
            taxable_amount=totals.taxable_amount,
            total_tax_amount=totals.total_tax_amount,
            grand_total_amount=totals.grand_total_amount,
        )
        self._session.add(invoice)
        await self._session.flush()
        await self.replace_invoice_lines(invoice.id, totals)
        return invoice

    async def replace_invoice_lines(self, invoice_id: uuid.UUID, totals: CalculatedInvoiceTotals) -> None:
        await self._session.execute(delete(FinanceTaxRecord).where(FinanceTaxRecord.invoice_id == invoice_id))
        await self._session.execute(delete(FinanceInvoiceLine).where(FinanceInvoiceLine.invoice_id == invoice_id))
        for line in totals.lines:
            self._session.add(
                FinanceInvoiceLine(
                    invoice_id=invoice_id,
                    line_number=line.line_number,
                    description=line.description,
                    hsn_sac=line.hsn_sac,
                    quantity=line.quantity,
                    unit_amount=line.unit_amount,
                    discount_amount=line.discount_amount,
                    taxable_amount=line.taxable_amount,
                    gst_rate_basis_points=line.gst_rate_basis_points,
                    cgst_amount=line.cgst_amount,
                    sgst_amount=line.sgst_amount,
                    igst_amount=line.igst_amount,
                    total_tax_amount=line.total_tax_amount,
                    line_total_amount=line.line_total_amount,
                    pricing_mode=line.pricing_mode,
                )
            )
        await self._session.flush()

    async def create_tax_records(self, invoice_id: uuid.UUID) -> None:
        lines = (
            await self._session.execute(
                select(FinanceInvoiceLine)
                .where(FinanceInvoiceLine.invoice_id == invoice_id)
                .order_by(FinanceInvoiceLine.line_number)
            )
        ).scalars()
        for line in lines:
            if line.cgst_amount:
                self._session.add(
                    FinanceTaxRecord(
                        invoice_id=invoice_id,
                        invoice_line_id=line.id,
                        tax_component="cgst",
                        taxable_amount=line.taxable_amount,
                        tax_rate_basis_points=line.gst_rate_basis_points // 2,
                        tax_amount=line.cgst_amount,
                    )
                )
            if line.sgst_amount:
                self._session.add(
                    FinanceTaxRecord(
                        invoice_id=invoice_id,
                        invoice_line_id=line.id,
                        tax_component="sgst",
                        taxable_amount=line.taxable_amount,
                        tax_rate_basis_points=line.gst_rate_basis_points // 2,
                        tax_amount=line.sgst_amount,
                    )
                )
            if line.igst_amount:
                self._session.add(
                    FinanceTaxRecord(
                        invoice_id=invoice_id,
                        invoice_line_id=line.id,
                        tax_component="igst",
                        taxable_amount=line.taxable_amount,
                        tax_rate_basis_points=line.gst_rate_basis_points,
                        tax_amount=line.igst_amount,
                    )
                )
        await self._session.flush()

    async def allocate_official_invoice_number(self, invoice: FinanceInvoice, *, division_code: str) -> str:
        result = await self._session.execute(
            select(FinanceInvoiceSeries)
            .where(
                FinanceInvoiceSeries.legal_entity_id == invoice.legal_entity_id,
                FinanceInvoiceSeries.gst_registration_id == invoice.gst_registration_id,
                FinanceInvoiceSeries.division_id == invoice.division_id,
                FinanceInvoiceSeries.financial_year == invoice.financial_year,
                FinanceInvoiceSeries.series_code == division_code,
            )
            .with_for_update()
        )
        series = result.scalar_one()
        series.last_number += 1
        invoice.invoice_series_id = series.id
        invoice.official_invoice_number = invoice_number(series.series_code, series.financial_year, series.last_number)
        return invoice.official_invoice_number

    async def allocate_brand_reference(self, invoice: FinanceInvoice, *, brand_code: str) -> str:
        result = await self._session.execute(
            select(FinanceBrandRefSeries)
            .where(
                FinanceBrandRefSeries.legal_entity_id == invoice.legal_entity_id,
                FinanceBrandRefSeries.division_id == invoice.division_id,
                FinanceBrandRefSeries.brand_id == invoice.brand_id,
                FinanceBrandRefSeries.financial_year == invoice.financial_year,
                FinanceBrandRefSeries.series_code == brand_code,
            )
            .with_for_update()
        )
        series = result.scalar_one()
        series.last_number += 1
        invoice.brand_ref_series_id = series.id
        invoice.brand_reference = invoice_number(series.series_code, series.financial_year, series.last_number)
        return invoice.brand_reference

    async def create_outbox_event(
        self,
        *,
        invoice: FinanceInvoice,
        idempotency_key: str,
        payload: dict[str, object],
        payload_sha256: str,
    ) -> None:
        self._session.add(
            FinanceOutboxEvent(
                organization_id=invoice.organization_id,
                legal_entity_id=invoice.legal_entity_id,
                division_id=invoice.division_id,
                brand_id=invoice.brand_id,
                aggregate_type="invoice",
                aggregate_id=invoice.id,
                event_type="finance.invoice.issued",
                idempotency_key=idempotency_key,
                payload_json=payload,
                payload_sha256=payload_sha256,
                status="pending",
            )
        )
        await self._session.flush()

    async def refresh_invoice(self, invoice: FinanceInvoice) -> FinanceInvoice:
        await self._session.refresh(invoice)
        return invoice


def invoice_result(invoice: FinanceInvoice, *, replayed: bool = False) -> InvoiceResult:
    return InvoiceResult(
        invoice_id=invoice.id,
        status=invoice.status,
        official_invoice_number=invoice.official_invoice_number,
        brand_reference=invoice.brand_reference,
        replayed=replayed,
    )
