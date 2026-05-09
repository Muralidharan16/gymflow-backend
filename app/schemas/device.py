from pydantic import BaseModel, UUID4
from typing import Optional
from datetime import datetime
from ..models.models import DeviceStatus

class DeviceRegisterRequest(BaseModel):
    branch_id: UUID4
    device_uid: str
    firmware_version: Optional[str] = None

class DeviceRegisterResponse(BaseModel):
    id: UUID4
    api_key: str

class DeviceStatusResponse(BaseModel):
    id: UUID4
    branch_id: UUID4
    device_uid: str
    status: DeviceStatus
    last_seen_at: Optional[datetime]
    firmware_version: Optional[str]
