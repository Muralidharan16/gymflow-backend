from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class FinanceCheckoutCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: str
    billing_interval: str
    billing_party_id: uuid.UUID


class FinanceCheckoutCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    checkout_fields: dict[str, str]
    display_amount: Decimal
    display_currency: str


class FinanceCheckoutStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finance_invoice_id: uuid.UUID
    finance_checkout_intent_id: uuid.UUID
    invoice_status: str
    checkout_intent_status: str
    payment_state: str
    display_amount: Decimal
    display_currency: str


class FinanceInternalPaymentApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    currency_code: str
    idempotency_key: str
    internal_actor: str
    reason: str


class FinanceInternalPaymentApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation_id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_status: str
    allocated_amount: Decimal
    replayed: bool = False


class FinanceAdminPaymentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: uuid.UUID
    invoice_id: uuid.UUID | None = None
    payment_state: str
    invoice_status: str | None = None
    provider_code: str | None = None
