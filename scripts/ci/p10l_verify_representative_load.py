#!/usr/bin/env python3
"""P10-L representative load budget verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUDGET_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.json"
DIGEST_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.sha256"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--postgres", required=True)
    parser.add_argument("--redis-stats", required=True)
    parser.add_argument("--redis-ping", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    budget_doc = _load(BUDGET_PATH)
    budgets = budget_doc["budgets"]["representative_http"]
    http = _load(Path(args.http))
    resources = _load(Path(args.resources))
    redis_stats = _load(Path(args.redis_stats))
    postgres_lines = [
        line.strip()
        for line in Path(args.postgres).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    redis_ping = Path(args.redis_ping).read_text(encoding="utf-8").strip()

    expected_digest = DIGEST_PATH.read_text(encoding="utf-8").strip()
    computed_digest = hashlib.sha256(
        json.dumps(
            budget_doc,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    requests = http["requests"]
    latency = http["latency"]
    overall = latency["overall"]
    writes = latency["writes"]

    require(computed_digest == expected_digest, "frozen performance budget digest mismatch")
    require(budget_doc.get("status") == "frozen", "performance budgets are not frozen")
    require(http.get("decision") == "CALIBRATION_PASS", "representative HTTP harness did not complete")
    require(requests["throughput_rps"] >= budgets["min_throughput_rps"], "throughput below frozen minimum")
    require(requests["http_errors"] <= budgets["max_http_errors"], "HTTP error budget exceeded")
    require(requests["server_errors_5xx"] <= budgets["max_server_errors_5xx"], "5xx budget exceeded")
    require(overall["p95_ms"] <= budgets["max_overall_p95_ms"], "overall p95 exceeded frozen maximum")
    require(overall["p99_ms"] <= budgets["max_overall_p99_ms"], "overall p99 exceeded frozen maximum")
    require(writes["p95_ms"] <= budgets["max_write_p95_ms"], "write p95 exceeded frozen maximum")
    require(writes["p99_ms"] <= budgets["max_write_p99_ms"], "write p99 exceeded frozen maximum")
    require(resources["cpu_percent"]["max"] <= budgets["max_cpu_percent"], "CPU exceeded frozen maximum")
    require(resources["rss_bytes"]["max"] <= budgets["max_rss_bytes"], "RSS exceeded frozen maximum")

    require(bool(postgres_lines), "PostgreSQL health evidence is empty")
    if postgres_lines:
        try:
            activity_count = int(postgres_lines[0])
        except ValueError:
            activity_count = -1
        require(activity_count > 0, "PostgreSQL post-load connectivity/activity proof failed")
    else:
        activity_count = -1

    require(redis_ping == "PONG", "Redis post-load ping failed")
    require(int(redis_stats.get("rejected_connections", 0)) == 0, "Redis rejected connections under load")

    record = {
        "schema_version": 1,
        "phase": "P10-L",
        "budget_digest": expected_digest,
        "representative_http": {
            "throughput_rps": requests["throughput_rps"],
            "http_errors": requests["http_errors"],
            "server_errors_5xx": requests["server_errors_5xx"],
            "overall_p50_ms": overall["p50_ms"],
            "overall_p95_ms": overall["p95_ms"],
            "overall_p99_ms": overall["p99_ms"],
            "write_p95_ms": writes["p95_ms"],
            "write_p99_ms": writes["p99_ms"],
        },
        "resources": {
            "cpu_percent_max": resources["cpu_percent"]["max"],
            "rss_bytes_max": resources["rss_bytes"]["max"],
        },
        "dependency_health": {
            "postgres_activity_count": activity_count,
            "redis_ping": redis_ping,
            "redis_rejected_connections": int(redis_stats.get("rejected_connections", 0)),
        },
        "provider_execution": "DEFERRED_FAIL_CLOSED",
        "decision": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    Path(args.output).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if errors:
        for error in errors:
            print(f"P10-L representative-load violation: {error}")
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    print("P10_REPRESENTATIVE_LOAD=PASS")
    print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
