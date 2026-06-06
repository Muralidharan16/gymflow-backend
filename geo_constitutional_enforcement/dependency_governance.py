"""
geo_constitutional_enforcement/dependency_governance.py
"""
import sys
import hashlib
import time
from pathlib import Path
from .result import ConstitutionalResult

LOCKFILE_PATH = Path("constitutional/dependency_lockfile.sha256")
REQUIREMENTS_PATH = Path("requirements.txt")

def compute_hash(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()

def verify() -> ConstitutionalResult:
    start_time = time.time()
    artifacts = []
    
    if not LOCKFILE_PATH.exists():
        return ConstitutionalResult(
            stage="dependency_governance",
            status="fail",
            duration_ms=int((time.time() - start_time) * 1000),
            artifacts=[],
            metadata={},
            error=f"No dependency lockfile found at {LOCKFILE_PATH}."
        )
        
    try:
        current_hash = compute_hash(REQUIREMENTS_PATH)
    except FileNotFoundError as e:
        return ConstitutionalResult(
            stage="dependency_governance",
            status="fail",
            duration_ms=int((time.time() - start_time) * 1000),
            artifacts=[],
            metadata={},
            error=str(e)
        )

    frozen_hash = LOCKFILE_PATH.read_text().strip().split()[0]
    
    if current_hash != frozen_hash:
        return ConstitutionalResult(
            stage="dependency_governance",
            status="fail",
            duration_ms=int((time.time() - start_time) * 1000),
            artifacts=[f"Frozen: {frozen_hash}", f"Current: {current_hash}"],
            metadata={"frozen_hash": frozen_hash, "current_hash": current_hash},
            error="Dependency lockfile drift detected. Requires constitutional amendment."
        )
        
    artifacts.append("Dependency lockfile verified.")
    return ConstitutionalResult(
        stage="dependency_governance",
        status="pass",
        duration_ms=int((time.time() - start_time) * 1000),
        artifacts=artifacts,
        metadata={"hash": current_hash}
    )

def freeze():
    try:
        current_hash = compute_hash(REQUIREMENTS_PATH)
        LOCKFILE_PATH.write_text(f"{current_hash}  {REQUIREMENTS_PATH.name}\n")
    except FileNotFoundError as e:
        sys.exit(1)

if __name__ == "__main__":
    if "--freeze" in sys.argv:
        freeze()
