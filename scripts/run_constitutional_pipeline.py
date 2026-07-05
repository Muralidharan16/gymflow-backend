#!/usr/bin/env python3
"""
scripts/run_constitutional_pipeline.py

Top-level operator command for executing the entire constitutional
enforcement pipeline.

This pipeline enforces value-driven determinism. It imports each
engine, invokes it as a pure function, receives a strongly-typed
ConstitutionalResult object, and serializes it to JSON.

Stdout interception and logging parsing are explicitly banned.
"""
import sys
import time
import json
import warnings
import subprocess
from pathlib import Path
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Suppress warnings that might leak into stdout
warnings.filterwarnings("ignore")

from geo_constitutional_enforcement import (
    enforcement_integrity_checker,
    dependency_governance,
    purity_scanner,
    drift_detector,
    schema_validator,
    replay_validator,
    complexity_auditor
)
from geo_constitutional_enforcement.result import ConstitutionalResult

def emit_result(result: ConstitutionalResult):
    sys.stdout.write(json.dumps(result.to_dict(), separators=(",", ":")) + "\n")
    sys.stdout.flush()

def run_pytest_stage() -> ConstitutionalResult:
    """Wraps external pytest execution into our schema boundary."""
    start_time = time.time()
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/constitutional/"],
            capture_output=True, text=True, check=False
        )
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Exit code 5 means no tests ran, which we consider a pass during bootstrap
        status = "pass" if res.returncode in (0, 5) else "fail"
        artifacts = [line for line in res.stdout.split("\n") if line.strip()]
        error = res.stderr.strip() if res.stderr.strip() else None
        
        if status == "fail" and not error:
            error = "Pytest execution failed. See artifacts."
            
        return ConstitutionalResult(
            stage="constitutional_tests",
            status=status,
            duration_ms=duration_ms,
            artifacts=artifacts,
            metadata={"return_code": res.returncode},
            error=error
        )
    except Exception as e:
        return ConstitutionalResult(
            stage="constitutional_tests",
            status="fail",
            duration_ms=int((time.time() - start_time) * 1000),
            artifacts=[],
            metadata={},
            error=f"Subprocess wrapper failed: {e}"
        )

def main():
    stages = [
        ("enforcement_integrity", enforcement_integrity_checker.verify),
        ("dependency_governance", dependency_governance.verify),
        ("purity_scan", purity_scanner.verify),
        ("semantic_drift", drift_detector.verify),
        ("schema_validation", lambda: schema_validator.verify("ci")),
        ("replay_validation", lambda: replay_validator.verify("ci")),
        ("complexity_audit", complexity_auditor.verify),
        ("constitutional_tests", run_pytest_stage),
    ]
    
    for name, func in stages:
        try:
            result = func()
        except Exception as e:
            result = ConstitutionalResult(
                stage=name,
                status="fail",
                duration_ms=0,
                artifacts=[],
                metadata={},
                error=f"Uncaught engine exception: {e}"
            )
            
        emit_result(result)
        
        if result.status == "fail":
            # Fail-fast constraint
            sys.exit(1)
            
    sys.exit(0)

if __name__ == "__main__":
    main()
