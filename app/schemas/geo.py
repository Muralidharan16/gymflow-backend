from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class CountryDTO(BaseModel):
    iso2: str
    name: str
    phone_code: Optional[str] = None
    ui_config: Optional[Dict[str, Any]] = None

class PostalLookupResult(BaseModel):
    postal_code_id: int
    postal_code: str
    locality: Optional[str] = None
    city_id: int
    city_name: str
    subdivision_id: int
    subdivision_name: str
    country_iso2: str
    timezone: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class CitySearchDTO(BaseModel):
    city_id: int
    city_name: str
    subdivision_name: str
    country_iso2: str

class SubdivisionSearchDTO(BaseModel):
    subdivision_id: int
    subdivision_name: str
    type: str
    country_iso2: str
