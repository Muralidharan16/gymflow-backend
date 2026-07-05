from abc import ABC, abstractmethod
from typing import Dict, Any, List

class BaseGeoParser(ABC):
    def __init__(self, parser_version: str):
        self.parser_version = parser_version
        
    @abstractmethod
    def parse(self, raw_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Parses raw payload into canonical insertion dictionaries.
        Must execute validation boundaries (bounds, orphans, duplicates).
        """
        pass
