from pydantic import BaseModel, ConfigDict, Field
import uuid
from decimal import Decimal
from typing import Optional

class TaxConfigBase(BaseModel):
    tax_type: str = "GST"
    tax_id: str = Field(..., min_length=5, max_length=50)
    legal_name: str
    gst_rate: Decimal = Decimal("18.00")
    sac_code: str = "996319"
    filing_frequency: Optional[str] = None

class TaxConfigCreate(TaxConfigBase):
    pass

class TaxConfigResponse(BaseModel):
    gym_id: uuid.UUID
    tax_type: str
    tax_id_masked: str
    legal_name: str
    gst_rate: Decimal
    sac_code: str
    is_active: bool
    model_config = ConfigDict(from_attributes=True)

class GymBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
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
