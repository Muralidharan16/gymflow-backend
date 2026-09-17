#!/usr/bin/env python3
"""P10-B production-shaped durable queue calibration.

This calibration measures the real Celery -> PostgreSQL branch outbox path with
reduced runtime identities. Synthetic malformed lifecycle commands are used so
there is no external provider effect; each command must converge exactly once
to the durable dead-letter terminal state. P10-Q separately owns worker
replacement and recovery certification.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import redis
from celery import Celery
from kombu.exceptions import OperationalError

ROOT = Path(__file__).resolve().parents[2]


def required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"P10-B durable queue calibration requires {name}")
    return value


def connect_url(name: str):
    parsed = urlparse(required(name))
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P10-B database host for {name}: {parsed.hostname}")
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def seed_authority(item_count: int) -> list[uuid.UUID]:
    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    with connect_url("TEST_ADMIN_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P10-B Durable Queue Calibration',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p10b-queue-{org_id.hex}"),
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    timezone,currency_code,region_code,country_code
                ) VALUES (
                    %s,%s,'P10-B Queue Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    branch_id,
                    org_id,
                    f"P10BQ-{branch_id.hex[:8]}",
                    f"p10b-queue-{branch_id.hex}",
                ),
            )
        connection.commit()

    ids = [uuid.uuid4() for _ in range(item_count)]
    with connect_url("P10B_APP_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_role','saga_orchestrator',true),
                    pg_catalog.set_config('app.current_user_id','',true),
                    pg_catalog.set_config('app.current_principal_type','',true),
                    pg_catalog.set_config('app.current_gym_id','',true)
                """,
                (str(org_id),),
            )
            cursor.executemany(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object('p10b_queue_calibration',%s),1,%s
                )
                """,
                [
                    (event_id, org_id, branch_id, sequence, uuid.uuid4())
                    for sequence, event_id in enumerate(ids, start=1)
                ],
            )
        connection.commit()
    return ids


def terminal_snapshot(ids: list[uuid.UUID]) -> dict[str, int]:
    with connect_url("TEST_ADMIN_DATABASE_URL") as connection:
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
                SELECT count(*)
                FROM public.branch_outbox_events
                WHERE outbox_id = ANY(%s)
                  AND status = 'dead_lettered'
                  AND attempt_count = 1
                  AND lease_fence = 1
                  AND leased_by IS NULL
                  AND leased_until IS NULL
                  AND last_error IS NOT NULL
                """,
                (ids,),
            )
            exact_terminal = int(cursor.fetchone()[0])
    states["exact_terminal"] = exact_terminal
    return states


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        required("CELERY_BROKER_URL"),
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        decode_responses=True,
    )


def controller() -> Celery:
    return Celery(
        "p10b-durable-queue-controller",
        broker=required("CELERY_BROKER_URL"),
        backend=required("CELERY_RESULT_BACKEND"),
    )


def worker_ping(hostname: str) -> bool:
    client = controller()
    try:
        replies = client.control.ping(destination=[hostname], timeout=1.5)
        return any(reply.get(hostname, {}).get("ok") == "pong" for reply in replies)
    except (redis.RedisError, OperationalError, OSError):
        return False
    finally:
        client.close()


def queue_depth(queue: str) -> int:
    client = redis_client()
    try:
        return int(client.llen(queue))
    finally:
        client.close()


def wait_for(predicate, description: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {description}")


def worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for forbidden in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "P10B_APP_DATABASE_URL",
    ):
        environment.pop(forbidden, None)
    environment.update(
        {
            "ENVIRONMENT": "test",
            "DOERS_PROCESS_PROFILE": "worker",
            "CELERY_WORKER_PROFILE": "worker",
            "NOTIFICATION_EMAIL_PROVIDER_MODE": "disabled",
            "P4C_RESEND_API_KEY": "",
            "RESEND_WEBHOOK_SECRET": "",
            "NOTIFICATION_METRICS_OTLP_ENDPOINT": "",
            "SEARCH_PROVIDER_MODE": "disabled",
            "OPENSEARCH_URL": "",
            "OPENSEARCH_USERNAME": "",
            "OPENSEARCH_PASSWORD": "",
            "SEARCH_METRICS_OTLP_ENDPOINT": "",
            "P4E_METRICS_OTLP_ENDPOINT": "",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", required=True)
    parser.add_argument("--worker-log", required=True)
    args = parser.parse_args()
    if args.items < 100 or args.items % 20 != 0:
        raise SystemExit("items must be >=100 and divisible by branch poller batch size 20")
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("concurrency must be in [1, 8]")

    client = redis_client()
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis broker did not answer PING")
    finally:
        client.close()

    ids = seed_authority(args.items)
    initial = terminal_snapshot(ids)
    if initial.get("pending") != args.items:
        raise RuntimeError(f"unexpected initial durable states: {initial}")

    token = uuid.uuid4().hex
    hostname = f"p10b-queue-{token}@localhost"
    queue = f"p10b-queue-{token}"
    log_path = Path(args.worker_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
            f"--concurrency={args.concurrency}",
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

    try:
        def ready() -> bool:
            if process.poll() is not None:
                handle.flush()
                raise RuntimeError(
                    "P10-B calibration worker exited during startup:\n"
                    + log_path.read_text(encoding="utf-8")
                )
            return worker_ping(hostname)

        wait_for(ready, "Celery calibration worker readiness", timeout=45.0)

        task_count = args.items // 20 + args.concurrency
        ctl = controller()
        started = time.perf_counter()
        try:
            for _ in range(task_count):
                ctl.send_task("app.tasks.branch_outbox_poller.run", queue=queue)
        finally:
            ctl.close()

        wait_for(
            lambda: terminal_snapshot(ids).get("exact_terminal") == args.items,
            "all durable queue rows to reach exact terminal state",
            timeout=90.0,
        )
        elapsed = time.perf_counter() - started
        final = terminal_snapshot(ids)
        wait_for(lambda: queue_depth(queue) == 0, "broker calibration queue to drain", timeout=15.0)

        if final != {"dead_lettered": args.items, "exact_terminal": args.items}:
            raise RuntimeError(f"durable terminal convergence mismatch: {final}")
        if process.poll() is not None:
            raise RuntimeError("calibration worker exited before evidence capture")

        record = {
            "schema_version": 1,
            "phase": "P10-B",
            "mode": "calibration",
            "surface": "celery_postgresql_branch_outbox",
            "synthetic_terminal_effect": "dead_lettered_malformed_lifecycle_command",
            "external_provider_effects": 0,
            "items": args.items,
            "worker_concurrency": args.concurrency,
            "poller_batch_size": 20,
            "task_count": task_count,
            "elapsed_seconds": round(elapsed, 6),
            "durable_items_per_second": round(args.items / elapsed, 3),
            "terminal_states": final,
            "broker_remaining": 0,
            "decision": "CALIBRATION_PASS",
        }
        Path(args.output).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(record, indent=2, sort_keys=True))
        print("P10B_DURABLE_QUEUE_CALIBRATION=PASS")
        return 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
