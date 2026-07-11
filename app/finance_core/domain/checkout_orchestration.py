from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.finance_core.domain.invoice_engine import InvoiceLineInput


class FinanceCheckoutOrchestrationError(Exception):
    pass


class FinanceCheckoutPlanNotFoundError(FinanceCheckoutOrchestrationError):
    pass


class FinanceCheckoutBillingIntervalError(FinanceCheckoutOrchestrationError):
    pass


@dataclass(frozen=True)
class CheckoutPlanSelector:
    plan_code: str
    billing_interval: str


@dataclass(frozen=True)
class CheckoutRedirectMetadata:
    success_url: str | None = None
    cancel_url: str | None = None


@dataclass(frozen=True)
class CreateCheckoutSessionCommand:
    organization_id: uuid.UUID | None
    billing_party_id: uuid.UUID
    selector: CheckoutPlanSelector
    idempotency_key: str
    redirect: CheckoutRedirectMetadata | None = None


@dataclass(frozen=True)
class ResolvedCheckoutPlan:
    plan_code: str
    billing_interval: str
    legal_entity_id: uuid.UUID
    gst_registration_id: uuid.UUID
    division_id: uuid.UUID
    brand_id: uuid.UUID
    currency_code: str
    supply_date: date
    line_items: tuple[InvoiceLineInput, ...]


@dataclass(frozen=True)
class SafeCheckoutSessionResult:
    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    provider_order_id: str
    checkout_fields: dict[str, str]
    display_amount: Decimal
    display_currency: str
    replayed: bool = False


class CheckoutPlanResolver(Protocol):
    async def resolve_plan(self, selector: CheckoutPlanSelector) -> ResolvedCheckoutPlan:
        """Resolve client-safe selectors into server-owned invoice inputs."""
