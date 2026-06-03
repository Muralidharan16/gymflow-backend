from typing import Dict, Any, List
from .base import BaseGeoParser

class IndiaPostParser(BaseGeoParser):
    def __init__(self):
        super().__init__(parser_version="1.0.0")

    def parse(self, raw_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Parses the official Indian Postal dataset.
        Enforces coordinate sanitization and locality normalization.
        """
        parsed_records = []
        for record in raw_payload:
            # 1. Coordinate Sanitization
            lat = record.get("latitude")
            lon = record.get("longitude")
            if lat and not (-90 <= float(lat) <= 90):
                lat = None
            if lon and not (-180 <= float(lon) <= 180):
                lon = None
                
            # 2. Canonical mapping
            parsed_records.append({
                "postal_code": str(record.get("Pincode")).strip(),
                "locality": str(record.get("OfficeName")).strip(),
                "city_name": str(record.get("Districtname")).strip(),
                "subdivision_name": str(record.get("statename")).strip(),
                "latitude": lat,
                "longitude": lon,
            })
            
        return parsed_records
