from pydantic import BaseModel
from typing import List, Optional
from ..models.models import SaaSPlanTier

class SaaSFeature(BaseModel):
    name: str
    description: str
    is_enabled: bool

class SaaSPlanRead(BaseModel):
    tier: SaaSPlanTier
    name: str
    price_monthly: float
    max_branches: int
    max_members: Optional[int]
    pan_required: bool
    features: List[SaaSFeature]

class SaaSPlanList(BaseModel):
    plans: List[SaaSPlanRead]
