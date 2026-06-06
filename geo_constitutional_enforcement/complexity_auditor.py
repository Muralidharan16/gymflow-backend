"""
geo_constitutional_enforcement/complexity_auditor.py
"""
import time
import yaml
from pathlib import Path
from .result import ConstitutionalResult

BUDGETS_MANIFEST = Path("constitutional/governance_budgets.yaml")
MANIFEST_DIR = Path("constitutional")

def verify_spec_version_exists(manifest_path: str) -> list[str]:
    violations = []
    try:
        manifest = yaml.safe_load(Path(manifest_path).read_text())
        version = manifest["version_policy"]["current"]
    except Exception as e:
        return [f"Failed to read spec_version_manifest: {e}"]
        
    version_dir = Path(f"constitutional/spec_versions/{version}")
    required_files = sorted(manifest["version_policy"].get("versioned_specs", []))
    
    for f in required_files:
        if not (version_dir / f).exists():
            violations.append(f"Spec version {version} missing file: {f}")
            
    return violations

def verify() -> ConstitutionalResult:
    start_time = time.time()
    
    if not BUDGETS_MANIFEST.exists():
        return ConstitutionalResult("complexity_audit", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing {BUDGETS_MANIFEST}")
        
    config = yaml.safe_load(BUDGETS_MANIFEST.read_text())
    budgets = config.get("budgets", {})
    
    violations = []
    
    for category in sorted(budgets.keys()):
        limits = budgets[category]
        for limit_key in sorted(limits.keys()):
            if limit_key.startswith("max_") and "current" in limits:
                if limits["current"] > limits[limit_key]:
                    violations.append(f"Budget '{category}' exceeded: {limits['current']} > {limits[limit_key]}")

    try:
        exemptions_config = yaml.safe_load((MANIFEST_DIR / "deterministic_purity_rules.yaml").read_text())
        exemptions = exemptions_config.get("exemptions", {})
        max_per_file = budgets.get("enforcement_exemptions", {}).get("max_per_file", 5)
        
        for file_path in sorted(exemptions.keys()):
            rules = exemptions[file_path]
            if len(rules) > max_per_file:
                violations.append(f"Exemption budget exceeded for {file_path}: {len(rules)} > {max_per_file}")
    except Exception:
        pass

    try:
        boundaries_config = yaml.safe_load((MANIFEST_DIR / "nondeterminism_boundaries.yaml").read_text())
        boundaries = boundaries_config.get("boundaries", [])
        max_boundaries = budgets.get("nondeterminism_boundaries", {}).get("max_modules", 6)
        
        if len(boundaries) > max_boundaries:
            violations.append(f"Boundary budget exceeded: {len(boundaries)} > {max_boundaries}")
    except Exception:
        pass

    manifest_path = "constitutional/spec_version_manifest.yaml"
    if Path(manifest_path).exists():
        violations.extend(verify_spec_version_exists(manifest_path))

    if violations:
        return ConstitutionalResult("complexity_audit", "fail", int((time.time() - start_time) * 1000), violations, {"violations": violations}, "Budgets exceeded")
        
    return ConstitutionalResult("complexity_audit", "pass", int((time.time() - start_time) * 1000), ["Complexity budgets verified."], {})
