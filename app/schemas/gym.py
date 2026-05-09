from pydantic import BaseModel, ConfigDict
import uuid
from decimal import Decimal
from typing import Optional

class TaxConfigBase(BaseModel):
    gst_number: str
    legal_name: str
    gst_rate: Decimal = Decimal("18.00")
    sac_code: str = "996319"

class TaxConfigCreate(TaxConfigBase):
    pass

class TaxConfigResponse(TaxConfigBase):
    gym_id: uuid.UUID
    is_active: bool
    model_config = ConfigDict(from_attributes=True)

class GymBase(BaseModel):
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None

class GymCreate(GymBase):
    pass

class GymUpdate(GymBase):
    name: Optional[str] = None

class GymResponse(GymBase):
    id: uuid.UUID
    org_id: uuid.UUID
    gymu_id: str
    is_active: bool
    model_config = ConfigDict(from_attributes=True)
