#!/usr/bin/env python3
"""Verify the immutable P10-B performance-budget freeze."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BUDGET_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.json"
DIGEST_PATH = ROOT / "docs/architecture/p10_performance_budgets.v1.sha256"

EXPECTED_DIGEST = "b8613f5deab4d1dba77ba41d86b4cb4d8e8dab6b85fa5630a6206105951b8520"
EXPECTED_SOURCE_SHA = "2c35d9c777f038b195f1191ffcaeeffbf14dc6e2"
EXPECTED_P9_SHA = "33abd2bad81c65ac998b91726cc314ab54080010"
EXPECTED_P9_TREE = "a6a0a87ebbaa251482c67f1bc4cbd9dc8ab7d1e0"
EXPECTED_ALEMBIC_HEAD = "zk07d8e9f0a45"

EXPECTED_BUDGETS = {
    "representative_http": {
        "min_throughput_rps": 45.0,
        "max_http_errors": 0,
        "max_server_errors_5xx": 0,
        "max_overall_p95_ms": 850.0,
        "max_overall_p99_ms": 1200.0,
        "max_write_p95_ms": 1100.0,
        "max_write_p99_ms": 1350.0,
        "max_cpu_percent": 125.0,
        "max_rss_bytes": 268435456,
    },
    "redis_transport": {
        "min_enqueue_items_per_second": 100000.0,
        "min_drain_items_per_second": 125000.0,
        "max_round_trip_seconds": 0.1,
        "max_remaining_items": 0,
    },
    "durable_queue": {
        "min_durable_items_per_second": 40.0,
        "min_exact_terminal_ratio": 1.0,
        "max_external_provider_effects": 0,
        "max_broker_remaining": 0,
    },
    "soak_stability": {
        "duration_seconds": 300,
        "max_rss_growth_bytes": 33554432,
        "max_db_connection_growth": 8,
        "max_db_connections": 32,
        "max_worker_broker_depth": 0,
        "max_throughput_degradation_ratio": 0.15,
        "max_overall_p95_growth_ratio": 1.2,
        "max_write_p95_growth_ratio": 1.2,
    },
}

EXPECTED_PROVENANCE = {
    "representative_http": {
        "workflow": "P10-B Representative Baseline Calibration",
        "run_id": 35242615083,
        "job_id": 105274898803,
        "artifact_id": 10505868726,
        "artifact_zip_sha256": "dc92bb5a31677d6f915ee170cdfd3f292fd584ca79be34090954123d8330e9bb",
    },
    "redis_transport": {
        "workflow": "P10-B Queue Calibration",
        "run_id": 35242615307,
        "job_id": 105274621590,
        "artifact_id": 10506132559,
        "artifact_zip_sha256": "d18d9b3928ac0c14583782639a823f2d9eef9937aa2414342e1cf567ff3bb5e2",
    },
    "durable_queue": {
        "workflow": "P10-B Durable Queue Calibration",
        "run_id": 35242615247,
        "job_id": 105274785468,
        "artifact_id": 10506815274,
        "artifact_zip_sha256": "1f8cb9a07224592b05dee0aa809c116313702ad48fa319ce6ceb66b8f9575336",
    },
}

EXPECTED_SCOPE = {
    "synthetic_only": True,
    "live_provider_credentials": False,
    "production_container": True,
    "production_process_profile": "api",
    "postgresql_major": 16,
    "real_redis": True,
    "refund_provider_execution": "DEFERRED_FAIL_CLOSED",
}

EXPECTED_CHANGE_POLICY = {
    "budget_loosening": "forbidden_after_freeze",
    "budget_tightening": "requires_recertification",
    "calibration_replacement": "requires_new_evidence_and_recertification",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_digest(document: dict[str, Any]) -> str:
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_document(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(document.get("schema_version") == 1, "schema_version must be 1")
    require(document.get("phase") == "P10-B", "phase must be P10-B")
    require(document.get("status") == "frozen", "status must be frozen")
    require(
        document.get("source_candidate_sha") == EXPECTED_SOURCE_SHA,
        "source candidate SHA changed; recalibration and recertification required",
    )
    require(
        document.get("p9_base") == {"sha": EXPECTED_P9_SHA, "tree": EXPECTED_P9_TREE},
        "P9 base binding changed",
    )
    require(
        document.get("alembic_head") == EXPECTED_ALEMBIC_HEAD,
        "Alembic head binding changed",
    )
    require(document.get("scope") == EXPECTED_SCOPE, "production-shaped scope changed")
    require(document.get("budgets") == EXPECTED_BUDGETS, "frozen numeric budgets changed")
    require(
        document.get("change_policy") == EXPECTED_CHANGE_POLICY,
        "budget change policy changed",
    )

    calibrations = document.get("calibrations")
    require(isinstance(calibrations, dict), "calibrations must be an object")
    if not isinstance(calibrations, dict):
        return errors
    require(set(calibrations) == set(EXPECTED_PROVENANCE), "calibration surfaces changed")

    for name, expected in EXPECTED_PROVENANCE.items():
        calibration = calibrations.get(name)
        require(isinstance(calibration, dict), f"{name}: calibration must be an object")
        if not isinstance(calibration, dict):
            continue
        require(
            calibration.get("candidate_sha") == EXPECTED_SOURCE_SHA,
            f"{name}: candidate SHA must match frozen source candidate",
        )
        for key, value in expected.items():
            require(calibration.get(key) == value, f"{name}: {key} provenance changed")
        digest = calibration.get("artifact_zip_sha256")
        require(
            isinstance(digest, str) and bool(SHA256_RE.fullmatch(digest)),
            f"{name}: invalid artifact SHA256",
        )

    http = calibrations.get("representative_http", {}).get("observed", {})
    hb = EXPECTED_BUDGETS["representative_http"]
    require(http.get("throughput_rps", -1) >= hb["min_throughput_rps"], "HTTP throughput below frozen minimum")
    require(http.get("http_errors", 1) <= hb["max_http_errors"], "HTTP errors exceed frozen maximum")
    require(http.get("server_errors_5xx", 1) <= hb["max_server_errors_5xx"], "HTTP 5xx exceeds frozen maximum")
    require(http.get("overall_p95_ms", float("inf")) <= hb["max_overall_p95_ms"], "HTTP p95 exceeds frozen maximum")
    require(http.get("overall_p99_ms", float("inf")) <= hb["max_overall_p99_ms"], "HTTP p99 exceeds frozen maximum")
    require(http.get("write_p95_ms", float("inf")) <= hb["max_write_p95_ms"], "write p95 exceeds frozen maximum")
    require(http.get("write_p99_ms", float("inf")) <= hb["max_write_p99_ms"], "write p99 exceeds frozen maximum")
    require(http.get("cpu_percent_max", float("inf")) <= hb["max_cpu_percent"], "CPU exceeds frozen maximum")
    require(http.get("rss_bytes_max", float("inf")) <= hb["max_rss_bytes"], "RSS exceeds frozen maximum")

    redis_obs = calibrations.get("redis_transport", {}).get("observed", {})
    rb = EXPECTED_BUDGETS["redis_transport"]
    require(redis_obs.get("enqueue_items_per_second", -1) >= rb["min_enqueue_items_per_second"], "Redis enqueue below frozen minimum")
    require(redis_obs.get("drain_items_per_second", -1) >= rb["min_drain_items_per_second"], "Redis drain below frozen minimum")
    require(redis_obs.get("round_trip_seconds", float("inf")) <= rb["max_round_trip_seconds"], "Redis round trip exceeds frozen maximum")
    require(redis_obs.get("remaining_items", 1) <= rb["max_remaining_items"], "Redis queue failed to drain")
    require(redis_obs.get("durable_business_authority") == "postgresql", "Redis must remain non-authoritative")

    durable = calibrations.get("durable_queue", {}).get("observed", {})
    db = EXPECTED_BUDGETS["durable_queue"]
    items = durable.get("items", 0)
    exact = durable.get("exact_terminal_items", -1)
    terminal_ratio = (exact / items) if isinstance(items, int) and items > 0 else -1.0
    require(durable.get("durable_items_per_second", -1) >= db["min_durable_items_per_second"], "durable queue throughput below frozen minimum")
    require(terminal_ratio >= db["min_exact_terminal_ratio"], "durable queue exact-terminal ratio below frozen minimum")
    require(exact == items and items > 0, "durable queue did not converge exactly once")
    require(durable.get("external_provider_effects", 1) <= db["max_external_provider_effects"], "durable calibration caused provider effects")
    require(durable.get("broker_remaining", 1) <= db["max_broker_remaining"], "durable broker queue did not drain")

    return errors


def main() -> int:
    document = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    errors = validate_document(document)
    computed = canonical_digest(document)
    sidecar = DIGEST_PATH.read_text(encoding="utf-8").strip()

    if computed != EXPECTED_DIGEST:
        errors.append(f"budget digest changed: expected {EXPECTED_DIGEST}, got {computed}")
    if sidecar != EXPECTED_DIGEST:
        errors.append(f"digest sidecar changed: expected {EXPECTED_DIGEST}, got {sidecar}")
    if computed != sidecar:
        errors.append("budget document and digest sidecar do not match")

    if errors:
        for error in errors:
            print(f"P10-B budget freeze violation: {error}", file=sys.stderr)
        return 1

    print(f"P10B_PERFORMANCE_BUDGET_DIGEST={computed}")
    print("P10_BASELINE_BUDGETS=PASS")
    print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
