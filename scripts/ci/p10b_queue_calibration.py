#!/usr/bin/env python3
"""P10-B synthetic Redis queue calibration.

Measures enqueue/drain throughput on an isolated Redis list. This is calibration
only: PostgreSQL remains the durable business authority and P10-Q separately
owns durable worker replacement/recovery certification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

import redis


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--items", type=int, default=5000)
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.items < 100:
        raise SystemExit("queue calibration requires at least 100 items")
    if not 32 <= args.payload_bytes <= 4096:
        raise SystemExit("payload-bytes must be in [32, 4096]")

    client = redis.Redis.from_url(args.redis_url, decode_responses=False)
    if client.ping() is not True:
        raise RuntimeError("Redis calibration endpoint did not answer PING")

    key = f"p10b:queue-calibration:{uuid.uuid4()}"
    payload = hashlib.sha256(key.encode("utf-8")).digest()
    payload = (payload * ((args.payload_bytes // len(payload)) + 1))[: args.payload_bytes]

    try:
        enqueue_started = time.perf_counter()
        pipe = client.pipeline(transaction=False)
        for sequence in range(args.items):
            envelope = sequence.to_bytes(8, "big") + payload
            pipe.rpush(key, envelope)
        enqueue_results = pipe.execute()
        enqueue_seconds = time.perf_counter() - enqueue_started
        if len(enqueue_results) != args.items or int(client.llen(key)) != args.items:
            raise RuntimeError("queue calibration enqueue cardinality mismatch")

        drain_started = time.perf_counter()
        pipe = client.pipeline(transaction=False)
        for _ in range(args.items):
            pipe.lpop(key)
        drained = pipe.execute()
        drain_seconds = time.perf_counter() - drain_started

        if len(drained) != args.items or any(item is None for item in drained):
            raise RuntimeError("queue calibration drain cardinality mismatch")
        if int(client.llen(key)) != 0:
            raise RuntimeError("queue calibration did not drain to zero")

        total_seconds = enqueue_seconds + drain_seconds
        record = {
            "schema_version": 1,
            "phase": "P10-B",
            "mode": "calibration",
            "queue_surface": "redis_list_synthetic_non_authoritative",
            "durable_business_authority": "postgresql",
            "items": args.items,
            "payload_bytes": args.payload_bytes + 8,
            "enqueue_seconds": round(enqueue_seconds, 6),
            "enqueue_items_per_second": round(args.items / enqueue_seconds, 3),
            "drain_seconds": round(drain_seconds, 6),
            "drain_items_per_second": round(args.items / drain_seconds, 3),
            "round_trip_seconds": round(total_seconds, 6),
            "remaining_items": 0,
            "decision": "CALIBRATION_PASS",
        }
        Path(args.output).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(record, indent=2, sort_keys=True))
        print("P10B_QUEUE_CALIBRATION=PASS")
        return 0
    finally:
        client.delete(key)


if __name__ == "__main__":
    raise SystemExit(main())
