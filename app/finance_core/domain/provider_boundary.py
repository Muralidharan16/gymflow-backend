from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class FinanceCheckoutIntentConflictError(Exception):
    pass


class FinanceCheckoutIntentStateError(Exception):
    pass


@dataclass(frozen=True)
class CreateCheckoutIntentCommand:
    organization_id: uuid.UUID | None
    invoice_id: uuid.UUID
    provider_code: str
    amount: Decimal
    currency_code: str
    idempotency_key: str


@dataclass(frozen=True)
class CheckoutIntentResult:
    intent_id: uuid.UUID
    invoice_id: uuid.UUID
    status: str
    amount: Decimal
    currency_code: str
    provider_code: str
    provider_order_ref: str | None
    replayed: bool = False


@dataclass(frozen=True)
class ProviderCheckoutIntentRequest:
    invoice_id: uuid.UUID
    amount: Decimal
    currency_code: str
    idempotency_key: str


@dataclass(frozen=True)
class ProviderCheckoutIntentResponse:
    provider_code: str
    provider_order_ref: str | None
    status: str


class CheckoutIntentProvider(Protocol):
    async def create_checkout_intent(
        self,
        request: ProviderCheckoutIntentRequest,
    ) -> ProviderCheckoutIntentResponse:
        """Deferred provider boundary; concrete adapters are intentionally not part of Phase 5E."""
