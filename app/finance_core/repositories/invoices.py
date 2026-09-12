from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance_core.domain.invoice_engine import (
    CalculatedInvoiceTotals,
    FinanceInvoiceConflictError,
    FinanceInvoiceNotFoundError,
    FinanceInvoiceStateError,
    FinanceInvoiceValidationError,
    InvoiceResult,
    invoice_number,
)
from app.finance_core.models.foundation import (
    FinanceBrandRefSeries,
    FinanceIdempotencyKey,
    FinanceInvoice,
    FinanceInvoiceLine,
    FinanceInvoiceSeries,
    FinanceOutboxEvent,
    FinanceTaxRecord,
)


@dataclass(frozen=True)
class FinanceInvoiceOrganization:
    id: uuid.UUID
    is_active: bool


@dataclass(frozen=True)
class FinanceInvoiceAccountingMasterData:
    organization_id: uuid.UUID
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID
    division_id: uuid.UUID
    brand_id: uuid.UUID
    billing_party_id: uuid.UUID
    seller_legal_name: str
    seller_pan: str | None
    seller_gstin: str
    seller_registered_address: str
    seller_state_code: str
    division_code: str
    brand_code: str
    buyer_billing_name: str
    buyer_address: str
    buyer_gstin: str | None
    buyer_pan: str | None
    buyer_place_of_supply_state_code: str
    buyer_gst_treatment: str


class FinanceInvoiceRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_invoice(self, invoice_id: uuid.UUID, *, for_update: bool = False) -> FinanceInvoice | None:
        statement = select(FinanceInvoice).where(FinanceInvoice.id == invoice_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def resolve_invoice_result(self, invoice_id: uuid.UUID) -> InvoiceResult | None:
        result = await self._session.execute(
            text(
                """
                SELECT invoice_id, status, official_invoice_number, brand_reference
                FROM app_secure.resolve_finance_invoice_result(:invoice_id)
                """
            ),
            {"invoice_id": invoice_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return InvoiceResult(
            invoice_id=row["invoice_id"],
            status=row["status"],
            official_invoice_number=row["official_invoice_number"],
            brand_reference=row["brand_reference"],
        )

    async def resolve_invoice_issue_context(
        self,
        invoice_id: uuid.UUID,
    ) -> dict[str, object] | None:
        result = await self._session.execute(
            text(
                """
                SELECT invoice_id, organization_id
                FROM app_secure.resolve_finance_invoice_issue_context(
                    :invoice_id
                )
                """
            ),
            {"invoice_id": invoice_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return dict(row)

    async def issue_invoice_atomic(
        self,
        *,
        invoice_id: uuid.UUID,
        idempotency_key: str,
    ) -> InvoiceResult:
        try:
            result = await self._session.execute(
                text(
                    """
                    SELECT
                        invoice_id,
                        invoice_status,
                        official_invoice_number,
                        brand_reference
                    FROM app_secure.issue_finance_invoice(
                        :invoice_id,
                        :idempotency_key
                    )
                    """
                ),
                {
                    "invoice_id": invoice_id,
                    "idempotency_key": idempotency_key,
                },
            )
        except DBAPIError as exc:
            original = exc.orig
            sqlstate = (
                getattr(original, "sqlstate", None)
                or getattr(original, "pgcode", None)
            )

            if sqlstate == "P4D21":
                raise FinanceInvoiceNotFoundError(
                    "Invoice was not found"
                ) from exc

            if sqlstate == "P4D22":
                raise FinanceInvoiceStateError(
                    "Only draft invoices can be issued"
                ) from exc

            if sqlstate == "P4D23":
                raise FinanceInvoiceValidationError(
                    "Invoice cannot be issued because persisted "
                    "finance data failed validation"
                ) from exc

            raise

        row = result.mappings().one()

        return InvoiceResult(
            invoice_id=row["invoice_id"],
            status=row["invoice_status"],
            official_invoice_number=(
                row["official_invoice_number"]
            ),
            brand_reference=row["brand_reference"],
            replayed=False,
        )

    async def get_organization(self, organization_id: uuid.UUID) -> FinanceInvoiceOrganization | None:
        result = await self._session.execute(
            text(
                """
                SELECT organization_id, is_active
                FROM app_secure.resolve_finance_invoice_organization(:organization_id)
                """
            ),
            {"organization_id": organization_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return FinanceInvoiceOrganization(id=row["organization_id"], is_active=row["is_active"])

    async def resolve_invoice_accounting_master_data(
        self,
        *,
        organization_id: uuid.UUID,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID,
        division_id: uuid.UUID,
        brand_id: uuid.UUID,
        billing_party_id: uuid.UUID,
        supply_date: date,
    ) -> FinanceInvoiceAccountingMasterData | None:
        result = await self._session.execute(
            text(
                """
                SELECT
                    organization_id, legal_entity_id, gst_registration_id, division_id, brand_id, billing_party_id,
                    seller_legal_name, seller_pan, seller_gstin, seller_registered_address, seller_state_code,
                    division_code, brand_code, buyer_billing_name, buyer_address, buyer_gstin, buyer_pan,
                    buyer_place_of_supply_state_code, buyer_gst_treatment
                FROM app_secure.resolve_finance_invoice_accounting_master_data(
                    :organization_id, :legal_entity_id, :gst_registration_id, :division_id, :brand_id, :billing_party_id, :supply_date
                )
                """
            ),
            {
                "organization_id": organization_id,
                "legal_entity_id": legal_entity_id,
                "gst_registration_id": gst_registration_id,
                "division_id": division_id,
                "brand_id": brand_id,
                "billing_party_id": billing_party_id,
                "supply_date": supply_date,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return FinanceInvoiceAccountingMasterData(**row)

    async def reserve_idempotency_key(
        self,
        *,
        organization_id: uuid.UUID | None,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[FinanceIdempotencyKey, bool]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        try:
            result = await self._session.execute(
                text(
                    """
                    SELECT id, organization_id, scope, idempotency_key, request_hash_sha256,
                           status, response_ref, created_at, expires_at, inserted
                    FROM app_secure.reserve_finance_idempotency(
                        :scope, :idempotency_key, :request_hash, :organization_id, :expires_at
                    )
                    """
                ),
                {
                    "scope": scope,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "organization_id": organization_id,
                    "expires_at": expires_at,
                },
            )
        except IntegrityError as exc:
            if "P4D finance idempotency request conflict" not in str(exc):
                raise
            raise FinanceInvoiceConflictError(
                "Invoice request conflicts with the existing idempotency key"
            ) from exc
        row = result.mappings().one()
        key = FinanceIdempotencyKey(
            id=row["id"],
            organization_id=row["organization_id"],
            scope=row["scope"],
            idempotency_key=row["idempotency_key"],
            request_hash_sha256=row["request_hash_sha256"],
            status=row["status"],
            response_ref=row["response_ref"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )
        return key, bool(row["inserted"])

    async def complete_idempotency_key(self, key: FinanceIdempotencyKey, *, response_ref: str) -> None:
        result = await self._session.execute(
            text(
                """
                SELECT id, organization_id, scope, idempotency_key, request_hash_sha256,
                       status, response_ref, created_at, expires_at
                FROM app_secure.complete_finance_idempotency(:idempotency_id, :response_ref)
                """
            ),
            {"idempotency_id": key.id, "response_ref": response_ref},
        )
        row = result.mappings().one()
        key.status = row["status"]
        key.response_ref = row["response_ref"]

    async def create_invoice(
        self,
        *,
        idempotency_key_id: uuid.UUID,
        organization_id: uuid.UUID | None,
        billing_party_id: uuid.UUID,
        legal_entity_id: uuid.UUID,
        gst_registration_id: uuid.UUID,
        division_id: uuid.UUID,
        brand_id: uuid.UUID,
        supply_date: date,
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
        line_payload = [
            {
                "line_number": line.line_number,
                "description": line.description,
                "hsn_sac": line.hsn_sac,
                "quantity": str(line.quantity),
                "unit_amount": str(line.unit_amount),
                "discount_amount": str(line.discount_amount),
                "taxable_amount": str(line.taxable_amount),
                "gst_rate_basis_points": line.gst_rate_basis_points,
                "cgst_amount": str(line.cgst_amount),
                "sgst_amount": str(line.sgst_amount),
                "igst_amount": str(line.igst_amount),
                "total_tax_amount": str(line.total_tax_amount),
                "line_total_amount": str(line.line_total_amount),
                "pricing_mode": line.pricing_mode,
            }
            for line in totals.lines
        ]
        result = await self._session.execute(
            text(
                """
                SELECT id, organization_id, billing_party_id, legal_entity_id, gst_registration_id,
                       division_id, brand_id, invoice_series_id, brand_ref_series_id, financial_year,
                       official_invoice_number, brand_reference, status, currency_code, seller_legal_name,
                       seller_gstin, seller_pan, seller_registered_address, seller_state_code,
                       buyer_billing_name, buyer_address, buyer_gstin, buyer_pan,
                       buyer_place_of_supply_state_code, buyer_gst_treatment, gst_supply_type,
                       subtotal_amount, discount_amount, taxable_amount, total_tax_amount, grand_total_amount,
                       issued_at, cancelled_at, metadata_json, created_at, updated_at
                FROM app_secure.persist_finance_draft_invoice(
                    :idempotency_key_id, :organization_id, :billing_party_id, :legal_entity_id,
                    :gst_registration_id, :division_id, :brand_id, :supply_date, :financial_year,
                    :currency_code, :seller_legal_name, :seller_gstin, :seller_pan,
                    :seller_registered_address, :seller_state_code, :buyer_billing_name,
                    :buyer_address, :buyer_gstin, :buyer_pan, :buyer_place_of_supply_state_code,
                    :buyer_gst_treatment, :gst_supply_type, :subtotal_amount, :discount_amount,
                    :taxable_amount, :total_tax_amount, :grand_total_amount, CAST(:lines AS jsonb)
                )
                """
            ),
            {
                "idempotency_key_id": idempotency_key_id,
                "organization_id": organization_id,
                "billing_party_id": billing_party_id,
                "legal_entity_id": legal_entity_id,
                "gst_registration_id": gst_registration_id,
                "division_id": division_id,
                "brand_id": brand_id,
                "supply_date": supply_date,
                "financial_year": financial_year,
                "currency_code": currency_code,
                "seller_legal_name": seller_legal_name,
                "seller_gstin": seller_gstin,
                "seller_pan": seller_pan,
                "seller_registered_address": seller_registered_address,
                "seller_state_code": seller_state_code,
                "buyer_billing_name": buyer_billing_name,
                "buyer_address": buyer_address,
                "buyer_gstin": buyer_gstin,
                "buyer_pan": buyer_pan,
                "buyer_place_of_supply_state_code": buyer_place_of_supply_state_code,
                "buyer_gst_treatment": buyer_gst_treatment,
                "gst_supply_type": totals.gst_supply_type,
                "subtotal_amount": totals.subtotal_amount,
                "discount_amount": totals.discount_amount,
                "taxable_amount": totals.taxable_amount,
                "total_tax_amount": totals.total_tax_amount,
                "grand_total_amount": totals.grand_total_amount,
                "lines": json.dumps(line_payload, separators=(",", ":")),
            },
        )
        row = result.mappings().one()
        return FinanceInvoice(
            id=row["id"],
            organization_id=row["organization_id"],
            billing_party_id=row["billing_party_id"],
            legal_entity_id=row["legal_entity_id"],
            gst_registration_id=row["gst_registration_id"],
            division_id=row["division_id"],
            brand_id=row["brand_id"],
            invoice_series_id=row["invoice_series_id"],
            brand_ref_series_id=row["brand_ref_series_id"],
            financial_year=row["financial_year"],
            official_invoice_number=row["official_invoice_number"],
            brand_reference=row["brand_reference"],
            status=row["status"],
            currency_code=row["currency_code"],
            seller_legal_name=row["seller_legal_name"],
            seller_gstin=row["seller_gstin"],
            seller_pan=row["seller_pan"],
            seller_registered_address=row["seller_registered_address"],
            seller_state_code=row["seller_state_code"],
            buyer_billing_name=row["buyer_billing_name"],
            buyer_address=row["buyer_address"],
            buyer_gstin=row["buyer_gstin"],
            buyer_pan=row["buyer_pan"],
            buyer_place_of_supply_state_code=row["buyer_place_of_supply_state_code"],
            buyer_gst_treatment=row["buyer_gst_treatment"],
            gst_supply_type=row["gst_supply_type"],
            subtotal_amount=row["subtotal_amount"],
            discount_amount=row["discount_amount"],
            taxable_amount=row["taxable_amount"],
            total_tax_amount=row["total_tax_amount"],
            grand_total_amount=row["grand_total_amount"],
            issued_at=row["issued_at"],
            cancelled_at=row["cancelled_at"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
