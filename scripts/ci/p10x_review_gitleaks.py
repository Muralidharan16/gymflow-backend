#!/usr/bin/env python3
"""Validate Gitleaks findings against exact reviewed fingerprint sets."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path


def fingerprint_digest(fingerprints: list[str]) -> str:
    payload = "".join(f"{item}\n" for item in sorted(fingerprints))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))

    if policy.get("schema_version") != 1:
        raise SystemExit("P10-X Gitleaks policy schema mismatch")
    suppressions = policy.get("suppressions")
    if not isinstance(suppressions, list) or not suppressions:
        raise SystemExit("P10-X Gitleaks review requires explicit reviewed suppressions")

    findings_by_rule: dict[str, list[dict[str, object]]] = defaultdict(list)
    for finding in report:
        rule = str(finding.get("RuleID", ""))
        fingerprint = str(finding.get("Fingerprint", ""))
        if not rule or not fingerprint:
            raise SystemExit("P10-X Gitleaks report contained a finding without rule/fingerprint")
        if finding.get("Secret") != "REDACTED":
            raise SystemExit("P10-X Gitleaks evidence must remain redacted")
        findings_by_rule[rule].append(finding)

    reviewed: dict[str, dict[str, object]] = {}
    today = date.today()
    for entry in suppressions:
        required = {
            "scanner",
            "rule_or_advisory_id",
            "exact_scope",
            "justification",
            "owner",
            "reviewed_at",
            "expires_at",
        }
        missing = required - set(entry)
        if missing:
            raise SystemExit(f"P10-X suppression missing fields: {sorted(missing)}")
        if entry["scanner"] != "gitleaks":
            raise SystemExit("P10-X suppression scanner must be gitleaks")
        rule = str(entry["rule_or_advisory_id"])
        if rule in reviewed:
            raise SystemExit(f"P10-X duplicate suppression rule: {rule}")
        expiry = date.fromisoformat(str(entry["expires_at"]))
        review_date = date.fromisoformat(str(entry["reviewed_at"]))
        if review_date > today:
            raise SystemExit(f"P10-X suppression review date is in the future: {rule}")
        if expiry < today:
            raise SystemExit(f"P10-X suppression expired: {rule}")
        reviewed[rule] = entry

    unexpected_rules = sorted(set(findings_by_rule) - set(reviewed))
    missing_rules = sorted(set(reviewed) - set(findings_by_rule))
    if unexpected_rules:
        raise SystemExit(f"P10-X unreviewed Gitleaks rules: {unexpected_rules}")
    if missing_rules:
        raise SystemExit(f"P10-X stale Gitleaks suppressions: {missing_rules}")

    decision_rules: dict[str, dict[str, object]] = {}
    for rule, entry in reviewed.items():
        findings = findings_by_rule[rule]
        fingerprints = [str(item["Fingerprint"]) for item in findings]
        scope = entry["exact_scope"]
        actual = {
            "finding_count": len(fingerprints),
            "unique_fingerprint_count": len(set(fingerprints)),
            "sorted_fingerprint_sha256": fingerprint_digest(fingerprints),
        }
        expected = {
            "finding_count": int(scope["finding_count"]),
            "unique_fingerprint_count": int(scope["unique_fingerprint_count"]),
            "sorted_fingerprint_sha256": str(scope["sorted_fingerprint_sha256"]),
        }
        if actual != expected:
            raise SystemExit(
                f"P10-X Gitleaks fingerprint set changed for {rule}: "
                f"actual={actual} expected={expected}"
            )
        decision_rules[rule] = {
            **actual,
            "reviewed_at": entry["reviewed_at"],
            "expires_at": entry["expires_at"],
            "owner": entry["owner"],
        }

    decision = {
        "schema_version": 1,
        "phase": "P10-X",
        "scanner": "gitleaks",
        "redacted_report": True,
        "finding_count": len(report),
        "rules": decision_rules,
        "decision": "PASS",
    }
    Path(args.output).write_text(
        json.dumps(decision, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(decision, indent=2, sort_keys=True))
    print("P10X_GITLEAKS_EXACT_REVIEW=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
