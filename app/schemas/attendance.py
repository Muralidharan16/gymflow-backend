from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class AccessVerifyRequest(BaseModel):
    fingerprint_id: str
    device_id: str


class AccessVerifyResponse(BaseModel):
    allowed: bool
    member_id: Optional[str]
    member_name: Optional[str]
    subscription_end: Optional[datetime]
    reason: Optional[str]
