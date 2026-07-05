from dataclasses import dataclass, asdict
from typing import Literal, List, Dict, Any, Optional

@dataclass
class ConstitutionalResult:
    stage: str
    status: Literal["pass", "fail"]
    duration_ms: int
    artifacts: List[str]
    metadata: Dict[str, Any]
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
