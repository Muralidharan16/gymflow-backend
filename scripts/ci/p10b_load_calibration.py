#!/usr/bin/env python3
"""P10-B representative HTTP load calibration.

This is a calibration harness, not a source of pass/fail performance budgets.
It exercises authenticated production API reads and writes against deterministic
synthetic tenants and emits machine-readable latency/throughput evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from jose import jwt


def deterministic_uuid(label: str) -> str:
    return str(uuid.UUID(hashlib.md5(label.encode("utf-8")).hexdigest()))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def token_for_tenant(secret_key: str, tenant_number: int) -> tuple[str, str, str]:
    org_id = deterministic_uuid(f"p9m-org-{tenant_number}")
    owner_id = deterministic_uuid(f"p10b-owner-{tenant_number}")
    email = f"p10b-owner-{tenant_number}@example.invalid"
    now = datetime.now(timezone.utc)
    payload = {
        "sub": owner_id,
        "principal_type": "owner",
        "org_id": org_id,
        "email": email,
        "role": "owner",
        "branch_ids": [],
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    return org_id, owner_id, jwt.encode(payload, secret_key, algorithm="HS256")


def request_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": org_id,
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1",
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--tenants", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.duration_seconds < 10:
        raise SystemExit("duration must be at least 10 seconds")
    if not 1 <= args.tenants <= 8:
        raise SystemExit("tenants must be in [1, 8]")
    if args.concurrency < 2:
        raise SystemExit("concurrency must be at least 2")

    tenant_auth: list[tuple[str, str, str]] = [
        token_for_tenant(args.secret_key, tenant_number)
        for tenant_number in range(1, args.tenants + 1)
    ]

    latencies_ms: list[float] = []
    read_latencies_ms: list[float] = []
    write_latencies_ms: list[float] = []
    status_counts: Counter[int] = Counter()
    method_counts: Counter[str] = Counter()
    exception_counts: Counter[str] = Counter()
    sequence = 0
    sequence_lock = asyncio.Lock()

    limits = httpx.Limits(
        max_connections=max(64, args.concurrency * 2),
        max_keepalive_connections=max(32, args.concurrency),
    )
    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=timeout,
        limits=limits,
        http2=False,
    ) as client:
        ready = await client.get("/_system/ready")
        if ready.status_code != 200 or ready.json() != {"status": "ready"}:
            raise RuntimeError(f"production API not ready: {ready.status_code} {ready.text}")

        # Authenticate and exercise every active synthetic tenant before timing.
        for tenant_number, (org_id, _owner_id, token) in enumerate(tenant_auth, start=1):
            response = await client.get(
                f"/organizations/{org_id}/members",
                params={"page": 1, "page_size": 50},
                headers=request_headers(token, org_id),
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"tenant {tenant_number} preflight failed: "
                    f"{response.status_code} {response.text}"
                )
            body = response.json()
            if body.get("total") != 500 or len(body.get("data", [])) != 50:
                raise RuntimeError(
                    f"tenant {tenant_number} baseline cardinality mismatch: {body!r}"
                )

        started = time.monotonic()
        deadline = started + args.duration_seconds

        async def next_sequence() -> int:
            nonlocal sequence
            async with sequence_lock:
                sequence += 1
                return sequence

        async def worker(worker_id: int) -> None:
            operation = 0
            while time.monotonic() < deadline:
                seq = await next_sequence()
                tenant_index = (worker_id + operation) % len(tenant_auth)
                tenant_number = tenant_index + 1
                org_id, _owner_id, token = tenant_auth[tenant_index]
                headers = request_headers(token, org_id)
                # One real write per ten operations keeps the workload mostly read
                # oriented while continuously exercising commit/counter behavior.
                is_write = seq % 10 == 0
                request_started = time.perf_counter()
                try:
                    if is_write:
                        branch_number = ((seq - 1) % 3) + 1
                        branch_id = deterministic_uuid(
                            f"p9m-branch-{tenant_number}-{branch_number}"
                        )
                        # 10 digits, deterministic and disjoint from the seed range.
                        phone = f"7{tenant_number:02d}{seq % 10_000_000:07d}"
                        payload = {
                            "name": f"P10-B Live Member {tenant_number}-{seq}",
                            "phone": phone,
                            "date_of_birth": "1992-01-01",
                            "emergency_contact_name": "9111111111",
                            "emergency_contact_phone": "9222222222",
                            "home_branch_id": branch_id,
                        }
                        response = await client.post(
                            f"/organizations/{org_id}/members",
                            json=payload,
                            headers=headers,
                        )
                        method_counts["POST"] += 1
                    else:
                        page = (seq % 10) + 1
                        response = await client.get(
                            f"/organizations/{org_id}/members",
                            params={"page": page, "page_size": 50},
                            headers=headers,
                        )
                        method_counts["GET"] += 1
                    elapsed_ms = (time.perf_counter() - request_started) * 1000.0
                    latencies_ms.append(elapsed_ms)
                    (write_latencies_ms if is_write else read_latencies_ms).append(elapsed_ms)
                    status_counts[response.status_code] += 1
                except Exception as exc:  # recorded as calibration evidence, then hard-failed below
                    elapsed_ms = (time.perf_counter() - request_started) * 1000.0
                    latencies_ms.append(elapsed_ms)
                    (write_latencies_ms if is_write else read_latencies_ms).append(elapsed_ms)
                    exception_counts[type(exc).__name__] += 1
                operation += 1

        await asyncio.gather(*(worker(worker_id) for worker_id in range(args.concurrency)))
        finished = time.monotonic()

    elapsed_seconds = finished - started
    request_count = sum(status_counts.values()) + sum(exception_counts.values())
    http_error_count = sum(count for code, count in status_counts.items() if code >= 400)
    server_error_count = sum(count for code, count in status_counts.items() if code >= 500)
    success_count = sum(count for code, count in status_counts.items() if 200 <= code < 300)

    if request_count == 0 or not read_latencies_ms or not write_latencies_ms:
        raise RuntimeError("calibration did not exercise both read and write traffic")
    if exception_counts:
        raise RuntimeError(f"calibration request exceptions: {dict(exception_counts)}")
    if http_error_count:
        raise RuntimeError(f"calibration HTTP errors: {dict(status_counts)}")
    if server_error_count:
        raise RuntimeError(f"calibration 5xx errors: {dict(status_counts)}")

    def latency_summary(values: list[float]) -> dict[str, float]:
        return {
            "count": len(values),
            "p50_ms": round(percentile(values, 0.50), 3),
            "p95_ms": round(percentile(values, 0.95), 3),
            "p99_ms": round(percentile(values, 0.99), 3),
            "max_ms": round(max(values), 3) if values else 0.0,
        }

    result = {
        "schema_version": 1,
        "phase": "P10-B",
        "mode": "calibration",
        "environment": {
            "tenants": args.tenants,
            "seed_members_per_tenant": 500,
            "page_size": 50,
            "concurrency": args.concurrency,
            "duration_seconds_requested": args.duration_seconds,
            "duration_seconds_observed": round(elapsed_seconds, 3),
            "read_write_mix": "9:1",
        },
        "requests": {
            "total": request_count,
            "success_2xx": success_count,
            "http_errors": http_error_count,
            "server_errors_5xx": server_error_count,
            "exceptions": dict(sorted(exception_counts.items())),
            "status_counts": {str(k): v for k, v in sorted(status_counts.items())},
            "method_counts": dict(sorted(method_counts.items())),
            "throughput_rps": round(request_count / elapsed_seconds, 3),
        },
        "latency": {
            "overall": latency_summary(latencies_ms),
            "reads": latency_summary(read_latencies_ms),
            "writes": latency_summary(write_latencies_ms),
        },
        "decision": "CALIBRATION_PASS",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    print("P10B_CALIBRATION_HTTP=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
