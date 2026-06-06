"""
geo_constitutional_enforcement/enforcement_integrity_checker.py
"""
import hashlib
import sys
import time
import yaml
from pathlib import Path
from .result import ConstitutionalResult

INTEGRITY_MANIFEST = Path("constitutional/enforcement_integrity.yaml")
HASH_FILE = Path("constitutional/enforcement_module_hashes.sha256")

def compute_module_hashes(config: dict) -> dict[str, str]:
    hashes = {}
    for module in config.get("critical_modules", []):
        path = Path(module["path"])
        if path.exists():
            content = path.read_bytes()
            hashes[module["path"]] = hashlib.sha256(content).hexdigest()
        else:
            hashes[module["path"]] = "MISSING"
    return hashes

def verify() -> ConstitutionalResult:
    start_time = time.time()
    
    if not INTEGRITY_MANIFEST.exists():
        return ConstitutionalResult("enforcement_integrity", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing {INTEGRITY_MANIFEST}")
        
    try:
        config = yaml.safe_load(INTEGRITY_MANIFEST.read_text())
    except Exception as e:
        return ConstitutionalResult("enforcement_integrity", "fail", int((time.time() - start_time) * 1000), [], {}, f"Parse error: {e}")
        
    current_hashes = compute_module_hashes(config)

    if not HASH_FILE.exists():
        return ConstitutionalResult("enforcement_integrity", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing {HASH_FILE}")

    frozen = {}
    for line in HASH_FILE.read_text().strip().splitlines():
        if not line.strip(): continue
        parts = line.split("  ", 1)
        if len(parts) == 2:
            h, path = parts
            frozen[path] = h

    changed = []
    missing = []
    
    # Deterministic sorting
    for path in sorted(current_hashes.keys()):
        h = current_hashes[path]
        if h == "MISSING": continue
        frozen_h = frozen.get(path)
        if frozen_h is None:
            missing.append(path)
        elif frozen_h != h:
            changed.append(path)

    if changed or missing:
        artifacts = []
        for p in changed: artifacts.append(f"MODIFIED: {p}")
        for p in missing: artifacts.append(f"UNTRACKED: {p}")
        return ConstitutionalResult(
            "enforcement_integrity", "fail", int((time.time() - start_time) * 1000), artifacts, 
            {"changed": changed, "untracked": missing}, "Critical modules modified"
        )

    return ConstitutionalResult("enforcement_integrity", "pass", int((time.time() - start_time) * 1000), ["Enforcement integrity verified."], {})

def freeze():
    if not INTEGRITY_MANIFEST.exists():
        sys.exit(1)
    config = yaml.safe_load(INTEGRITY_MANIFEST.read_text())
    hashes = compute_module_hashes(config)
    lines = []
    for path, h in sorted(hashes.items()):
        if h != "MISSING":
            lines.append(f"{h}  {path}")
    HASH_FILE.write_text("\n".join(lines) + "\n")

if __name__ == "__main__":
    if "--freeze" in sys.argv:
        freeze()
