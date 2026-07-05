from pydantic import BaseModel, Field, ConfigDict, model_validator
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

    @model_validator(mode='after')
    def validate_schedules(self) -> 'BulkOperatingHoursRequest':
        from collections import defaultdict
        
        # Group by day_of_week
        by_day = defaultdict(list)
        for sched in self.schedules:
            by_day[sched.day_of_week].append(sched)
            
        for day, slots in by_day.items():
            has_24 = any(s.is_24_hours for s in slots)
            has_closed = any(s.is_closed for s in slots)
            if (has_24 or has_closed) and len(slots) > 1:
                raise ValueError(f"Day {day} cannot have both 24-hours/Closed and specific time slots.")
                
            active_slots = [s for s in slots if not s.is_closed and not s.is_24_hours]
            for s in active_slots:
                if s.open_time is None or s.close_time is None:
                    raise ValueError(f"Missing open or close time for slot on day {day}.")
                if s.open_time >= s.close_time:
                    raise ValueError("Overnight hours are not supported yet. Split the schedule or use same-day time ranges.")
                    
            # Check overlap
            sorted_slots = sorted(active_slots, key=lambda x: x.open_time)
            for i in range(len(sorted_slots) - 1):
                if sorted_slots[i].close_time > sorted_slots[i+1].open_time:
                    raise ValueError("Overlapping slots within the same day are not allowed.")
                    
        return self

class BulkSpecialHoursRequest(BaseModel):
    schedules: List[BranchSpecialHoursCreate]

    @model_validator(mode='after')
    def validate_special_schedules(self) -> 'BulkSpecialHoursRequest':
        from collections import defaultdict
        
        # Group by special_date
        by_date = defaultdict(list)
        for sched in self.schedules:
            by_date[sched.special_date].append(sched)
            
        for s_date, slots in by_date.items():
            has_24 = any(s.is_24_hours for s in slots)
            has_closed = any(s.is_closed for s in slots)
            if (has_24 or has_closed) and len(slots) > 1:
                raise ValueError(f"Special date {s_date} cannot have both 24-hours/Closed and specific time slots.")
                
            active_slots = [s for s in slots if not s.is_closed and not s.is_24_hours]
            for s in active_slots:
                if s.open_time is None or s.close_time is None:
                    raise ValueError(f"Missing open or close time for special date {s_date}.")
                if s.open_time >= s.close_time:
                    raise ValueError("Overnight hours are not supported yet. Split the schedule or use same-day time ranges.")
                    
            # Check overlap
            sorted_slots = sorted(active_slots, key=lambda x: x.open_time)
            for i in range(len(sorted_slots) - 1):
                if sorted_slots[i].close_time > sorted_slots[i+1].open_time:
                    raise ValueError("Overlapping slots within the same day are not allowed.")
                    
        return self
