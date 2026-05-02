from pydantic import BaseModel
from typing import Optional


class DeviceRegisterRequest(BaseModel):
    device_ip: str
    device_model: Optional[str] = None


class DeviceRegisterResponse(BaseModel):
    id: str
    auth_token: str


class DeviceStatusResponse(BaseModel):
    id: str
    device_ip: str
    device_model: Optional[str]
    last_connected: Optional[str]
    status: str
