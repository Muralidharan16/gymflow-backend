from __future__ import annotations

import argparse
import json
from pathlib import Path


FORBIDDEN = {
    "request_id",
    "correlation_id",
    "tenant_id",
    "branch_id",
    "principal_id",
    "saga_id",
    "task_id",
    "trace_id",
    "span_id",
}


def _records(path: Path) -> list[dict]:
    result: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            result.append(record)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    args = parser.parse_args()
    path = Path(args.capture)
    if not path.is_file():
        raise SystemExit(f"P8-O worker capture missing: {path}")

    records = _records(path)
    p8 = [r for r in records if str(r.get("metric", "")).startswith("doers.")]
    if not p8:
        raise SystemExit("P8-O worker capture contains no P8 metrics")

    for record in p8:
        keys = set(record.get("attributes", {}))
        leaked = keys & FORBIDDEN
        if leaked:
            raise SystemExit(f"P8-O worker metric leaked forbidden labels: {sorted(leaked)}")

    redeliveries = [
        record
        for record in p8
        if record.get("metric") == "doers.queue.redeliveries"
        and float(record.get("value", 0.0)) >= 1.0
    ]
    if not redeliveries:
        raise SystemExit("P8-O observed no real Celery redelivery metric")

    workers = [
        record
        for record in p8
        if record.get("metric") == "doers.queue.worker.available"
        and record.get("attributes", {}).get("profile") == "worker"
        and float(record.get("value", 0.0)) >= 1.0
    ]
    if not workers:
        raise SystemExit("P8-O observed no live worker availability metric")

    heartbeats = [
        record
        for record in p8
        if record.get("metric") == "doers.platform.observability.heartbeat"
        and record.get("attributes", {}).get("profile") == "worker"
        and float(record.get("value", 0.0)) >= 1.0
    ]
    if not heartbeats:
        raise SystemExit("P8-O observed no worker observability heartbeat")

    print("P8O_WORKER_REDELIVERY_CAPTURE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
