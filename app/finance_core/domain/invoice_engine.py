from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


MONEY_PLACES = Decimal("0.01")


class FinanceInvoiceConflictError(Exception):
    pass


class FinanceInvoiceNotFoundError(Exception):
    pass


class FinanceInvoiceStateError(Exception):
    pass


class FinanceInvoiceValidationError(Exception):
    pass


@dataclass(frozen=True)
class InvoiceLineInput:
    description: str
    quantity: Decimal
    unit_price: Decimal
    hsn_sac: str | None
    gst_rate_basis_points: int
    pricing_mode: str
    discount_amount: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class CreateDraftInvoiceCommand:
    organization_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID
    division_id: uuid.UUID
    brand_id: uuid.UUID
    billing_party_id: uuid.UUID
    currency_code: str
    supply_date: date
    line_items: tuple[InvoiceLineInput, ...]
    idempotency_key: str


@dataclass(frozen=True)
class IssueInvoiceCommand:
    invoice_id: uuid.UUID
    idempotency_key: str


@dataclass(frozen=True)
class CalculatedInvoiceLine:
    line_number: int
    description: str
    hsn_sac: str | None
    quantity: Decimal
    unit_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    gst_rate_basis_points: int
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    line_total_amount: Decimal
    pricing_mode: str


@dataclass(frozen=True)
class CalculatedInvoiceTotals:
    subtotal_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    total_tax_amount: Decimal
    grand_total_amount: Decimal
    gst_supply_type: str
    lines: tuple[CalculatedInvoiceLine, ...]


@dataclass(frozen=True)
class InvoiceResult:
    invoice_id: uuid.UUID
    status: str
    official_invoice_number: str | None
    brand_reference: str | None
    replayed: bool = False


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def financial_year_for(value: date) -> str:
    start_year = value.year if value.month >= 4 else value.year - 1
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def calculate_invoice_totals(
    *,
    supplier_state_code: str,
    buyer_place_of_supply_state_code: str,
    line_items: tuple[InvoiceLineInput, ...],
) -> CalculatedInvoiceTotals:
    if not line_items:
        raise FinanceInvoiceValidationError("Invoice requires at least one line item")

    gst_supply_type = "intra_state" if supplier_state_code == buyer_place_of_supply_state_code else "inter_state"
    lines: list[CalculatedInvoiceLine] = []
    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")
    taxable_total = Decimal("0.00")
    tax_total = Decimal("0.00")
    grand_total = Decimal("0.00")

    for index, item in enumerate(line_items, start=1):
        if item.quantity <= 0:
            raise FinanceInvoiceValidationError("Line quantity must be positive")
        if item.unit_price < 0 or item.discount_amount < 0 or item.gst_rate_basis_points < 0:
            raise FinanceInvoiceValidationError("Line amounts and GST rate must be non-negative")
        if item.pricing_mode not in {"tax_exclusive", "tax_inclusive"}:
            raise FinanceInvoiceValidationError("Unsupported pricing mode")

        gross = money(item.quantity * item.unit_price)
        discount = money(item.discount_amount)
        if discount > gross:
            raise FinanceInvoiceValidationError("Line discount cannot exceed gross amount")

        rate = Decimal(item.gst_rate_basis_points) / Decimal(10000)
        if item.pricing_mode == "tax_inclusive":
            line_total = money(gross - discount)
            taxable = money(line_total / (Decimal("1.00") + rate)) if rate else line_total
            total_tax = money(line_total - taxable)
        else:
            taxable = money(gross - discount)
            total_tax = money(taxable * rate)
            line_total = money(taxable + total_tax)

        if gst_supply_type == "intra_state":
            cgst = money(total_tax / Decimal("2"))
            sgst = money(total_tax - cgst)
            igst = Decimal("0.00")
        else:
            cgst = Decimal("0.00")
            sgst = Decimal("0.00")
            igst = total_tax

        lines.append(
            CalculatedInvoiceLine(
                line_number=index,
                description=item.description,
                hsn_sac=item.hsn_sac,
                quantity=item.quantity,
                unit_amount=money(item.unit_price),
                discount_amount=discount,
                taxable_amount=taxable,
                gst_rate_basis_points=item.gst_rate_basis_points,
                cgst_amount=cgst,
                sgst_amount=sgst,
                igst_amount=igst,
                total_tax_amount=total_tax,
                line_total_amount=line_total,
                pricing_mode=item.pricing_mode,
            )
        )
        subtotal += gross
        discount_total += discount
        taxable_total += taxable
        tax_total += total_tax
        grand_total += line_total

    return CalculatedInvoiceTotals(
        subtotal_amount=money(subtotal),
        discount_amount=money(discount_total),
        taxable_amount=money(taxable_total),
        total_tax_amount=money(tax_total),
        grand_total_amount=money(grand_total),
        gst_supply_type=gst_supply_type,
        lines=tuple(lines),
    )


def invoice_number(series_code: str, financial_year: str, number: int) -> str:
    if number < 1:
        raise FinanceInvoiceValidationError("Invoice sequence number must be positive")
    value = f"{series_code}/{financial_year}/{number:05d}"
    if len(value) > 16:
        raise FinanceInvoiceValidationError("Invoice number exceeds GST-safe length target")
    return value
