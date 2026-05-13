from pydantic import BaseModel, ConfigDict
import uuid
from datetime import datetime, date
from typing import Optional
from app.models.enums import CheckInMethod, AttendanceDenialReason

class AttendanceBase(BaseModel):
    check_in_method: CheckInMethod

class AttendanceCreate(AttendanceBase):
    member_id: uuid.UUID

class CheckInRequest(BaseModel):
    member_id: uuid.UUID

class AttendanceResponse(AttendanceBase):
    id: uuid.UUID
    gym_id: uuid.UUID
    member_id: Optional[uuid.UUID] = None
    scan_time: datetime
    check_out_time: Optional[datetime] = None
    access_granted: bool
    denial_reason: Optional[AttendanceDenialReason] = None
    model_config = ConfigDict(from_attributes=True)

class AccessCheckResponse(BaseModel):
    granted: bool
    member_name: Optional[str] = None
    gym_id: Optional[str] = None
    end_date: Optional[str] = None
    reason: Optional[str] = None
