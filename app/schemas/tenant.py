from pydantic import BaseModel, UUID4
from typing import Optional
from ..models.models import StaffRole

class TenantContext(BaseModel):
    org_id: UUID4
    primary_branch_id: Optional[UUID4]
    role: StaffRole
    staff_id: UUID4
