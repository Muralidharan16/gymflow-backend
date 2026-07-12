from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal


class FinancePaymentApplicationGateError(Exception):
    pass


class FinancePaymentApplicationAuthorityError(FinancePaymentApplicationGateError):
    pass


@dataclass(frozen=True)
class ApplyConfirmedPaymentCommand:
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    currency_code: str
    idempotency_key: str
    internal_actor: str
    reason: str


@dataclass(frozen=True)
class AppliedPaymentResult:
    allocation_id: uuid.UUID
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_status: str
    allocated_amount: Decimal
    replayed: bool = False
