from pydantic import BaseModel, ConfigDict
from typing import List, Dict, Optional, Any
from datetime import datetime

class ImportPreviewResponse(BaseModel):
    import_id: str
    headers: List[str]
    preview_rows: List[Dict[str, Any]]
    total_rows: int

class ImportConfirmRequest(BaseModel):
    column_mapping: Dict[str, str]

class ImportLogResponse(BaseModel):
    import_id: str
    gym_id: str
    status: str
    total_rows: int
    successful_rows: int
    failed_rows: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
