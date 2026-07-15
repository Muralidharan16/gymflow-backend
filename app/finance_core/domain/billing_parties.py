from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping


BILLING_PARTY_CREATION_SOURCE = "finance_internal_billing_party_creation"
BILLING_PARTY_SYNTHETIC_PURPOSE = "razorpay_webhook_smoke"
BILLING_PARTY_SYNTHETIC_PHASE = "6AN-P2B"


@dataclass(frozen=True)
class BillingPartyCreationCommand:
    organization_id: uuid.UUID
    actor_organization_id: uuid.UUID
    billing_name: str
    party_type: str
    gst_treatment: str
    billing_address: str
    place_of_supply_state_code: str
    idempotency_key: str
    source: str = BILLING_PARTY_CREATION_SOURCE
    status: str = "active"
    gstin: str | None = None
    pan: str | None = None
    metadata: Mapping[str, Any] | None = None
    synthetic_mode: bool = True


@dataclass(frozen=True)
class BillingPartyCreationResult:
    billing_party_id: uuid.UUID
    organization_id: uuid.UUID
    billing_label: str
    party_type: str
    gst_treatment: str
    status: str
    replayed: bool
    synthetic_mode: bool
    metadata_summary: dict[str, object]


@dataclass(frozen=True)
class FinanceBillingPartyError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
