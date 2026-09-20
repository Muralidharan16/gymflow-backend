from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class FinanceOfflinePaymentPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    payment_method: Literal["cash", "bank_transfer", "cheque"]
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency_code: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    reference_code: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$",
    )
    proof_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class FinanceOfflinePaymentPrepareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_payment_request_id: uuid.UUID
    invoice_id: uuid.UUID
    status: str
    amount: Decimal
    currency_code: str
    payment_method: str
    reference_code: str
    prepared_actor_id: uuid.UUID
    replayed: bool


class FinanceOfflinePaymentApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_payment_request_id: uuid.UUID
    payment_id: uuid.UUID
    allocation_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_status: str
    status: str
    replayed: bool


class FinanceOfflinePaymentRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    )


class FinanceOfflinePaymentRejectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_payment_request_id: uuid.UUID
    status: str
    rejection_reason_code: str
    replayed: bool
