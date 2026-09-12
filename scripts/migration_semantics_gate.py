#!/usr/bin/env python3
"""Fail closed on migration-risk drift relative to the reviewed P2D lineage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from migration_semantics_inventory import MIGRATION_ROOT, scan


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "security" / "migration_risk_baseline.v1.json"


def _high_key(finding) -> str:
    return "|".join(
        (
            finding.path,
            finding.function,
            str(finding.line),
            finding.category,
            finding.snippet,
        )
    )


def main() -> int:
    paths = sorted(
        path for path in MIGRATION_ROOT.glob("*.py") if path.name != "__init__.py"
    )
    findings = scan(paths)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != 1:
        raise SystemExit("migration risk baseline schema is unsupported")

    critical = [item for item in findings if item.severity == "critical"]
    if critical:
        print("P2E migration risk gate FAILED: critical findings are never allowlisted")
        for item in critical:
            print(f"- {item.path}:{item.line} {item.category}: {item.snippet}")
        return 1

    high = [item for item in findings if item.severity == "high"]
    payload = "\n".join(sorted(_high_key(item) for item in high)) + "\n"
    digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    expected_count = int(baseline["reviewed_high_finding_count"])
    expected_digest = str(baseline["reviewed_high_findings_digest"])
    if len(high) != expected_count or digest != expected_digest:
        print("P2E migration risk gate FAILED: reviewed high-risk lineage changed")
        print(f"expected_count={expected_count} actual_count={len(high)}")
        print(f"expected_digest={expected_digest}")
        print(f"actual_digest={digest}")
        print("Any new, removed, moved, or modified high-risk construct requires explicit review.")
        return 1

    counts = {
        severity: sum(item.severity == severity for item in findings)
        for severity in ("critical", "high", "medium", "info")
    }
    print(
        "P2E migration risk gate passed "
        f"migrations={len(paths)} high_digest={digest} counts={counts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
