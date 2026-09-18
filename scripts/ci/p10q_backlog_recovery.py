#!/usr/bin/env python3
"""P10-Q durable backlog drain and worker-replacement certification."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ci.p10b_durable_queue_calibration import (  # noqa: E402
    connect_url,
    controller,
    queue_depth,
    redis_client,
    seed_authority,
    worker_environment,
    worker_ping,
)

POLL_BATCH_SIZE = 20


@dataclass
class Worker:
    process: subprocess.Popen[str]
    hostname: str
    queue: str
    handle: TextIO
    log: Path


def wait_for(predicate, description: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}; last={last!r}")


def snapshot(ids: list[uuid.UUID]) -> dict[str, int]:
    with connect_url("WORKER_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, count(*)
                FROM public.branch_outbox_events
                WHERE outbox_id = ANY(%s)
                GROUP BY status
                """,
                (ids,),
            )
            states = {str(status): int(count) for status, count in cursor.fetchall()}
            cursor.execute(
                """
                SELECT
                  count(*) FILTER (
                    WHERE status='dead_lettered'
                      AND attempt_count=1
                      AND leased_by IS NULL
                      AND leased_until IS NULL
                      AND last_error IS NOT NULL
                  ),
                  coalesce(max(lease_fence),0)
                FROM public.branch_outbox_events
                WHERE outbox_id = ANY(%s)
                """,
                (ids,),
            )
            exact_terminal, max_fence = cursor.fetchone()
    states["exact_terminal"] = int(exact_terminal)
    states["max_lease_fence"] = int(max_fence)
    return states


def start_worker(queue: str, concurrency: int, log_path: Path) -> Worker:
    token = uuid.uuid4().hex
    hostname = f"p10q-{token}@localhost"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.core.celery_app:celery_app",
            "worker",
            "--pool=prefork",
            f"--concurrency={concurrency}",
            "--prefetch-multiplier=1",
            f"--queues={queue}",
            f"--hostname={hostname}",
            "--without-gossip",
            "--without-mingle",
            "--loglevel=WARNING",
        ],
        cwd=ROOT,
        env=worker_environment(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def ready() -> bool:
        if process.poll() is not None:
            handle.flush()
            raise RuntimeError(
                f"P10-Q worker exited during startup:\n{log_path.read_text(encoding='utf-8')}"
            )
        return worker_ping(hostname)

    wait_for(ready, f"worker {hostname} readiness", timeout=45.0)
    return Worker(process=process, hostname=hostname, queue=queue, handle=handle, log=log_path)


def stop_worker(worker: Worker, *, hard: bool = False) -> None:
    if worker.process.poll() is None:
        os.killpg(worker.process.pid, signal.SIGKILL if hard else signal.SIGTERM)
        try:
            worker.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(worker.process.pid, signal.SIGKILL)
            worker.process.wait(timeout=5)
    worker.handle.close()


def send_poll_tasks(queue: str, count: int) -> None:
    ctl = controller()
    try:
        for _ in range(count):
            ctl.send_task("app.tasks.branch_outbox_poller.run", queue=queue)
    finally:
        ctl.close()


def sample(ids: list[uuid.UUID], queue: str, started: float) -> dict[str, int | float]:
    states = snapshot(ids)
    return {
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "broker_ready": queue_depth(queue),
        "pending": states.get("pending", 0),
        "processing": states.get("processing", 0),
        "dead_lettered": states.get("dead_lettered", 0),
        "exact_terminal": states.get("exact_terminal", 0),
        "max_lease_fence": states.get("max_lease_fence", 0),
    }


def expire_processing_leases(ids: list[uuid.UUID]) -> int:
    """Accelerate the production lease-expiry boundary for disposable CI.

    The production lease is intentionally long (10 minutes). P10-Q certifies
    replacement/reclaim behavior inside a bounded CI window by moving only the
    already-owned synthetic rows past their lease deadline. The replacement
    worker must still reclaim them through the real fenced claim path.
    """
    with connect_url("WORKER_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.branch_outbox_events
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE outbox_id = ANY(%s)
                  AND status='processing'
                  AND leased_by IS NOT NULL
                  AND leased_until > pg_catalog.clock_timestamp()
                RETURNING outbox_id
                """,
                (ids,),
            )
            expired = len(cursor.fetchall())
        connection.commit()
    return expired


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=800)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeseries", required=True)
    parser.add_argument("--worker-log-dir", required=True)
    args = parser.parse_args()

    if args.items < 400 or args.items % POLL_BATCH_SIZE:
        raise SystemExit("items must be >=400 and divisible by 20")
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("concurrency must be in [1,8]")

    client = redis_client()
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis broker did not answer PING")
    finally:
        client.close()

    ids = seed_authority(args.items)
    initial = snapshot(ids)
    if initial.get("pending") != args.items:
        raise RuntimeError(f"unexpected initial durable backlog: {initial}")

    log_dir = Path(args.worker_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    queue = f"p10q-backlog-{uuid.uuid4().hex}"
    samples: list[dict[str, int | float]] = []
    overall_started = time.perf_counter()
    first = start_worker(queue, args.concurrency, log_dir / "worker-1.log")
    second: Worker | None = None
    task_count = (args.items // POLL_BATCH_SIZE) * 2 + args.concurrency

    try:
        send_poll_tasks(queue, task_count)

        def first_progress() -> bool:
            samples.append(sample(ids, queue, overall_started))
            current = samples[-1]
            return int(current["dead_lettered"]) >= POLL_BATCH_SIZE

        wait_for(first_progress, "first worker durable progress", timeout=30.0)
        before_replacement = snapshot(ids)
        remaining_before = (
            before_replacement.get("pending", 0) + before_replacement.get("processing", 0)
        )
        if remaining_before <= 0:
            raise RuntimeError("first worker drained entire backlog before replacement proof")

        first_hostname = first.hostname
        stop_worker(first)

        after_stop = snapshot(ids)
        remaining_after_stop = after_stop.get("pending", 0) + after_stop.get("processing", 0)
        if remaining_after_stop <= 0:
            raise RuntimeError("no durable backlog remained at worker replacement boundary")

        processing_at_stop = int(after_stop.get("processing", 0))
        expired_leases = expire_processing_leases(ids)
        if expired_leases != processing_at_stop:
            raise RuntimeError(
                "lease-expiry injection did not cover every processing row: "
                f"processing={processing_at_stop} expired={expired_leases}"
            )

        second = start_worker(queue, args.concurrency, log_dir / "worker-2.log")
        if second.hostname == first_hostname:
            raise RuntimeError("replacement worker identity did not change")

        replacement_started = time.perf_counter()
        replacement_start_terminal = snapshot(ids).get("exact_terminal", 0)
        replacement_work = args.items - replacement_start_terminal
        send_poll_tasks(queue, task_count)

        deadline = time.monotonic() + 90.0
        while True:
            current = sample(ids, queue, overall_started)
            samples.append(current)
            if int(current["exact_terminal"]) == args.items:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"backlog drain deadline exceeded: {current}")
            time.sleep(0.1)

        replacement_elapsed = time.perf_counter() - replacement_started
        wait_for(lambda: queue_depth(queue) == 0, "broker ready queue drain", timeout=20.0)
        final = snapshot(ids)
        final_sample = sample(ids, queue, overall_started)
        samples.append(final_sample)

        expected_terminal = {
            "dead_lettered": args.items,
            "exact_terminal": args.items,
        }
        for key, expected in expected_terminal.items():
            if final.get(key) != expected:
                raise RuntimeError(f"durable terminal mismatch {key}: {final}")

        replacement_rate = replacement_work / replacement_elapsed
        if replacement_rate < 40.0:
            raise RuntimeError(
                f"replacement durable drain rate {replacement_rate:.3f}/s below frozen 40/s minimum"
            )
        if int(final_sample["broker_ready"]) != 0:
            raise RuntimeError("broker ready queue did not drain")
        if int(final_sample["pending"]) or int(final_sample["processing"]):
            raise RuntimeError("durable backlog did not converge to zero")

        timeseries_path = Path(args.timeseries)
        timeseries_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in samples),
            encoding="utf-8",
        )
        record = {
            "schema_version": 1,
            "phase": "P10-Q",
            "surface": "celery_postgresql_branch_outbox",
            "items": args.items,
            "worker_concurrency": args.concurrency,
            "worker_replacement": {
                "first_hostname": first_hostname,
                "replacement_hostname": second.hostname,
                "durable_backlog_at_stop": remaining_after_stop,
                "processing_at_stop": processing_at_stop,
                "ci_expired_processing_leases": expired_leases,
                "lease_expiry_fault_injection": True,
            },
            "replacement_drain": {
                "items": replacement_work,
                "elapsed_seconds": round(replacement_elapsed, 6),
                "items_per_second": round(replacement_rate, 3),
                "deadline_seconds": 90,
            },
            "queue_depth_samples": len(samples),
            "final_states": final,
            "broker_remaining": 0,
            "external_provider_effects": 0,
            "durable_business_authority": "postgresql",
            "provider_execution": "DEFERRED_FAIL_CLOSED",
            "decision": "PASS",
        }
        Path(args.output).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(record, indent=2, sort_keys=True))
        print("P10_QUEUE_BACKLOG_RECOVERY=PASS")
        print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
        return 0
    finally:
        if second is not None:
            stop_worker(second)
        elif first.handle and not first.handle.closed:
            stop_worker(first, hard=True)


if __name__ == "__main__":
    raise SystemExit(main())
