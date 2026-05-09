from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class AccessScanRequest(BaseModel):
    fingerprint_id: str


class AccessScanResponse(BaseModel):
    access_granted: bool
    reason: str
    member_name: Optional[str] = None
    member_uid: Optional[str] = None
    subscription_end: Optional[str] = None
