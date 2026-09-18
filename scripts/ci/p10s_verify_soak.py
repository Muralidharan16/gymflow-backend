#!/usr/bin/env python3
"""P10-S frozen-budget sustained-stability verifier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    frozen = json.loads(
        (ROOT / "docs/architecture/p10_performance_budgets.v1.json").read_text(
            encoding="utf-8"
        )
    )["budgets"]
    representative = frozen["representative_http"]
    soak = frozen["soak_stability"]

    http = json.loads(Path(args.http).read_text(encoding="utf-8"))
    worker = json.loads(Path(args.worker).read_text(encoding="utf-8"))
    resources = [
        json.loads(line)
        for line in Path(args.resources).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    windows = http.get("windows", [])
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(
        int(http.get("duration_seconds", 0)) >= int(soak["duration_seconds"]),
        "soak duration below frozen target",
    )
    require(len(windows) >= 5, "soak windows below P10-S minimum")

    for window in windows:
        number = window.get("window", "?")
        require(
            int(window.get("http_errors", 1)) <= representative["max_http_errors"]
            and int(window.get("server_errors_5xx", 1))
            <= representative["max_server_errors_5xx"]
            and not window.get("exceptions"),
            f"window {number} request errors",
        )
        overall = window["latency"]["overall"]
        writes = window["latency"]["writes"]
        require(
            float(overall["p95_ms"]) <= representative["max_overall_p95_ms"]
            and float(overall["p99_ms"]) <= representative["max_overall_p99_ms"],
            f"window {number} overall latency exceeds frozen budget",
        )
        require(
            float(writes["p95_ms"]) <= representative["max_write_p95_ms"]
            and float(writes["p99_ms"]) <= representative["max_write_p99_ms"],
            f"window {number} write latency exceeds frozen budget",
        )

    throughput_first = throughput_last = throughput_min_later = 0.0
    overall_p95_growth_ratio = write_p95_growth_ratio = 0.0
    if len(windows) >= 2:
        throughput_first = float(windows[0]["throughput_rps"])
        throughput_last = float(windows[-1]["throughput_rps"])
        throughput_min_later = min(float(w["throughput_rps"]) for w in windows[1:])
        minimum_sustained = throughput_first * (
            1.0 - float(soak["max_throughput_degradation_ratio"])
        )
        require(
            throughput_min_later >= minimum_sustained,
            "sustained throughput progressively degraded beyond frozen budget",
        )

        first_overall_p95 = float(windows[0]["latency"]["overall"]["p95_ms"])
        last_overall_p95 = float(windows[-1]["latency"]["overall"]["p95_ms"])
        first_write_p95 = float(windows[0]["latency"]["writes"]["p95_ms"])
        last_write_p95 = float(windows[-1]["latency"]["writes"]["p95_ms"])
        overall_p95_growth_ratio = (
            last_overall_p95 / first_overall_p95 if first_overall_p95 > 0 else float("inf")
        )
        write_p95_growth_ratio = (
            last_write_p95 / first_write_p95 if first_write_p95 > 0 else float("inf")
        )
        require(
            overall_p95_growth_ratio <= float(soak["max_overall_p95_growth_ratio"]),
            "overall p95 progressively regressed beyond frozen growth budget",
        )
        require(
            write_p95_growth_ratio <= float(soak["max_write_p95_growth_ratio"]),
            "write p95 progressively regressed beyond frozen growth budget",
        )

    rss_growth = db_connection_growth = 0
    max_rss = max_cpu = max_db_connections = max_broker_depth = 0
    if not resources:
        errors.append("no resource samples")
    else:
        max_rss = max(int(row["rss_bytes"]) for row in resources)
        max_cpu = max(float(row["cpu_percent"]) for row in resources)
        max_db_connections = max(int(row["db_connections"]) for row in resources)
        max_broker_depth = max(int(row["worker_broker_depth"]) for row in resources)

        require(max_rss <= representative["max_rss_bytes"], "RSS exceeded frozen maximum")
        require(
            max_cpu <= representative["max_cpu_percent"],
            "CPU exceeded frozen maximum",
        )
        require(
            max_db_connections <= int(soak["max_db_connections"]),
            "database connections exceeded frozen maximum",
        )
        require(
            max_broker_depth <= int(soak["max_worker_broker_depth"]),
            "worker broker backlog exceeded frozen maximum",
        )

        baseline_slice = resources[: min(3, len(resources))]
        terminal_slice = resources[-min(3, len(resources)) :]
        rss_growth = max(int(row["rss_bytes"]) for row in terminal_slice) - min(
            int(row["rss_bytes"]) for row in baseline_slice
        )
        db_connection_growth = max(
            int(row["db_connections"]) for row in terminal_slice
        ) - min(int(row["db_connections"]) for row in baseline_slice)
        require(
            rss_growth <= int(soak["max_rss_growth_bytes"]),
            "RSS progressively grew beyond frozen growth budget",
        )
        require(
            db_connection_growth <= int(soak["max_db_connection_growth"]),
            "database connections progressively grew beyond frozen growth budget",
        )

    require(
        worker.get("continuous_worker") is True
        and int(worker.get("external_provider_effects", 1)) == 0
        and int(worker.get("final_broker_depth", 1)) == 0,
        "worker/backlog soak invariant failed",
    )
    if resources:
        require(
            int(resources[-1]["worker_broker_depth"]) == 0,
            "final sampled worker broker backlog nonzero",
        )

    record = {
        "schema_version": 1,
        "phase": "P10-S",
        "duration_seconds": http.get("duration_seconds", 0),
        "windows": len(windows),
        "resource_samples": len(resources),
        "max_rss_bytes": max_rss,
        "rss_growth_bytes": rss_growth,
        "max_cpu_percent": max_cpu,
        "max_db_connections": max_db_connections,
        "db_connection_growth": db_connection_growth,
        "max_worker_broker_depth": max_broker_depth,
        "worker_items": worker.get("items_processed", 0),
        "throughput_first_rps": throughput_first,
        "throughput_last_rps": throughput_last,
        "throughput_min_later_rps": throughput_min_later,
        "overall_p95_growth_ratio": round(overall_p95_growth_ratio, 6),
        "write_p95_growth_ratio": round(write_p95_growth_ratio, 6),
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
            print("P10-S violation: " + error)
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    print("P10_SOAK=PASS")
    print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
