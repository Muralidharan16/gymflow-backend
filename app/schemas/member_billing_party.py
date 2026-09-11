from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class MemberBillingPartyUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_address: str = Field(min_length=1)
    place_of_supply_state_code: str = Field(pattern=r"^[0-9]{2}$")


class MemberBillingPartyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_party_id: uuid.UUID
    organization_id: uuid.UUID
    member_id: uuid.UUID
    buyer_kind: str
    party_type: str
    gst_treatment: str
