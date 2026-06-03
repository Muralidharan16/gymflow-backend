from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict
from datetime import date, time, datetime
import uuid

class OperatingHoursSlot(BaseModel):
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    is_closed: bool = False
    is_24_hours: bool = False

class OperatingHoursCreate(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday, 6=Sunday")
    slot_index: int = Field(default=1, ge=1)
    valid_from: date
    valid_until: Optional[date] = None
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    is_closed: bool = False
    is_24_hours: bool = False

class BranchSpecialHoursCreate(BaseModel):
    special_date: date
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    is_closed: bool = False
    is_24_hours: bool = False
    reason: Optional[str] = Field(None, max_length=255)

class HoursResponseBase(BaseModel):
    id: uuid.UUID
    day_of_week: int
    slot_index: int
    valid_from: date
    valid_until: Optional[date]
    open_time: Optional[time]
    close_time: Optional[time]
    is_closed: bool
    is_24_hours: bool
    is_overnight: bool
    
    model_config = ConfigDict(from_attributes=True)

class OrganizationOperatingHoursResponse(HoursResponseBase):
    org_id: uuid.UUID

class BranchOperatingHoursResponse(HoursResponseBase):
    branch_id: uuid.UUID

class BranchSpecialHoursResponse(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    special_date: date
    open_time: Optional[time]
    close_time: Optional[time]
    is_closed: bool
    is_24_hours: bool
    is_overnight: bool
    reason: Optional[str]
    
    model_config = ConfigDict(from_attributes=True)

class BranchHoursProjectionResponse(BaseModel):
    branch_id: uuid.UUID
    timezone: str
    current_status: str
    next_open_at: Optional[datetime]
    next_close_at: Optional[datetime]
    weekly_schedule: dict
    upcoming_exceptions: List[dict]
    
    model_config = ConfigDict(from_attributes=True)

class BulkOperatingHoursRequest(BaseModel):
    schedules: List[OperatingHoursCreate]

class BulkSpecialHoursRequest(BaseModel):
    schedules: List[BranchSpecialHoursCreate]
