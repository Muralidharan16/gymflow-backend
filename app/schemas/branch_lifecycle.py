from pydantic import BaseModel, ConfigDict, Field
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

class BranchTransitionRequest(BaseModel):
    to_status: str = Field(..., description="Target status definition code")
    reason: Optional[str] = Field(None, description="Optional or mandatory status transition reason")

class BranchStatusStateResponse(BaseModel):
    branch_id: uuid.UUID
    org_id: uuid.UUID
    status: str
    is_operational: bool
    status_changed_at: datetime
    status_changed_by: Optional[uuid.UUID] = None
    status_reason: Optional[str] = None
    lifecycle_transition_in_progress: bool
    saga_last_checkpoint: Optional[str] = None
    saga_compensation_strategy: Optional[str] = None
    watchdog_recovered_at: Optional[datetime] = None
    watchdog_recovery_count: int
    search_visibility_version: int
    search_last_synced_at: Optional[datetime] = None
    search_sync_failed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class BranchStatusHistoryResponse(BaseModel):
    history_id: uuid.UUID
    branch_id: uuid.UUID
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[uuid.UUID] = None
    changed_at: datetime
    reason: Optional[str] = None
    transition_source: Optional[str] = None
    snapshot: Dict[str, Any]
    correlation_id: Optional[uuid.UUID] = None
    correlation_emitted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class BranchWatchdogAlertResponse(BaseModel):
    alert_id: uuid.UUID
    branch_id: uuid.UUID
    alert_type: str
    triggered_at: datetime
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
