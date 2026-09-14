from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse

import psycopg
import redis
from celery import Celery
from kombu.exceptions import OperationalError


ROOT = Path(__file__).resolve().parents[1]
_DATABASE = "gymflow_p6b_test"
_TIMEOUT = 75.0


@dataclass(frozen=True)
class _Worker:
    process: subprocess.Popen[str]
    hostname: str
    queue: str
    log: Path


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"P6-B runtime requires {name}")
    return value


def _safe_topology() -> None:
    if os.environ.get("P6B_PROCESS_FAULTS") != "1":
        raise RuntimeError("P6-B destructive broker faults require explicit CI enablement")
    if os.environ.get("P6B_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P6-B disposable database acknowledgement is absent")

    database = urlparse(_required("TEST_ADMIN_DATABASE_URL"))
    if database.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P6-B database host: {database.hostname}")
    if database.path.lstrip("/") != _DATABASE:
        raise RuntimeError(f"unsafe P6-B database: {database.path!r}")

    broker = urlparse(_required("CELERY_BROKER_URL"))
    if broker.scheme != "rediss" or broker.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("P6-B broker must be disposable local rediss:// Redis")
    if broker.port != 16379:
        raise RuntimeError(f"unsafe P6-B broker port: {broker.port}")


def _admin_connect():
    parsed = urlparse(_required("TEST_ADMIN_DATABASE_URL"))
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def _wait_for(
    predicate: Callable[[], bool],
    *,
    description: str,
    timeout: float = _TIMEOUT,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {description}")


def _seed_branch() -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    with _admin_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P6-B Broker Recovery Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p6b-{org_id.hex}"),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    timezone,currency_code,region_code,country_code
                ) VALUES (
                    %s,%s,'P6-B Broker Recovery Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    branch_id,
                    org_id,
                    f"P6B-{branch_id.hex[:8]}",
                    f"p6b-{branch_id.hex}",
                ),
            )
        connection.commit()
    return org_id, branch_id


def _insert_durable_obligation(
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
    label: str,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    with _admin_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object('p6b_fault',CAST(%s AS text)),3,%s
                )
                """,
                (event_id, org_id, branch_id, label, uuid.uuid4()),
            )
        connection.commit()
    return event_id


def _state(event_id: uuid.UUID) -> tuple[object, ...]:
    with _admin_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,max_attempts,lease_fence,
                       leased_by,leased_until,last_error
                FROM public.branch_outbox_events
                WHERE outbox_id=%s
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            return row


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        _required("CELERY_BROKER_URL"),
        socket_connect_timeout=0.75,
        socket_timeout=0.75,
        decode_responses=True,
    )


def _redis_ping() -> bool:
    try:
        return _redis_client().ping() is True
    except (redis.RedisError, OSError):
        return False


def _docker(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _stop_broker() -> None:
    result = _docker("stop", "-t", "1", "p6b-primary")
    if result.returncode != 0:
        raise AssertionError(f"failed to stop P6-B Redis primary:\n{result.stdout}")
    _wait_for(lambda: not _redis_ping(), description="real Redis broker outage", timeout=15)


def _start_broker() -> None:
    result = _docker("start", "p6b-primary")
    if result.returncode != 0:
        raise AssertionError(f"failed to restart P6-B Redis primary:\n{result.stdout}")
    _wait_for(_redis_ping, description="real Redis broker recovery", timeout=25)


def _controller() -> Celery:
    return Celery(
        "p6b-runtime-controller",
        broker=_required("CELERY_BROKER_URL"),
        backend=_required("CELERY_RESULT_BACKEND"),
    )


def _worker_ping(hostname: str) -> bool:
    if not _redis_ping():
        return False
    client = _controller()
    try:
        replies = client.control.ping(destination=[hostname], timeout=1.5)
        return any(reply.get(hostname, {}).get("ok") == "pong" for reply in replies)
    except (redis.RedisError, OperationalError, OSError):
        return False
    finally:
        client.close()


def _send_poller(queue: str) -> None:
    client = _controller()
    try:
        client.send_task(
            "app.tasks.branch_outbox_poller.run",
            queue=queue,
            retry=True,
            retry_policy={
                "max_retries": 5,
                "interval_start": 0,
                "interval_step": 0.2,
                "interval_max": 1,
            },
        )
    finally:
        client.close()


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for forbidden in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "MIGRATION_PASSWORD",
    ):
        environment.pop(forbidden, None)
    environment.update(
        {
            "ENVIRONMENT": "production",
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


@contextmanager
def _running_worker(tmp_path: Path) -> Iterator[_Worker]:
    token = uuid.uuid4().hex
    hostname = f"p6b-{token}@localhost"
    queue = f"p6b-{token}"
    log = tmp_path / f"p6b-worker-{token}.log"
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.core.celery_app:celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            f"--queues={queue}",
            f"--hostname={hostname}",
            "--without-gossip",
            "--without-mingle",
            "--loglevel=INFO",
            "--include=app.tasks.branch_outbox_poller",
        ],
        cwd=ROOT,
        env=_worker_environment(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    worker = _Worker(process=process, hostname=hostname, queue=queue, log=log)
    try:
        def ready() -> bool:
            if process.poll() is not None:
                handle.flush()
                raise AssertionError(
                    "P6-B production worker exited during startup:\n"
                    + log.read_text(encoding="utf-8")
                )
            return _worker_ping(hostname)

        _wait_for(ready, description="production Celery worker readiness", timeout=45)
        yield worker
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


def test_live_worker_reconnects_and_postgresql_durable_work_survives_broker_restart(
    tmp_path: Path,
) -> None:
    _safe_topology()
    assert _redis_ping() is True

    org_id, branch_id = _seed_branch()
    pre_outage = _insert_durable_obligation(org_id, branch_id, "pre-outage")

    with _running_worker(tmp_path) as worker:
        original_pid = worker.process.pid
        assert _worker_ping(worker.hostname) is True

        _stop_broker()
        try:
            _wait_for(
                lambda: worker.process.poll() is None,
                description="same worker remains alive during broker outage",
                timeout=3,
            )
            assert worker.process.pid == original_pid
            time.sleep(2.0)
            assert worker.process.poll() is None
            assert worker.process.pid == original_pid
            assert _worker_ping(worker.hostname) is False

            during_outage = _insert_durable_obligation(
                org_id,
                branch_id,
                "during-outage",
            )
            for event_id in (pre_outage, during_outage):
                status, attempts, _max_attempts, fence, leased_by, leased_until, _ = _state(event_id)
                assert (status, attempts, fence, leased_by, leased_until) == (
                    "pending",
                    0,
                    0,
                    None,
                    None,
                )
        finally:
            _start_broker()

        assert worker.process.poll() is None
        assert worker.process.pid == original_pid
        _wait_for(
            lambda: _worker_ping(worker.hostname),
            description="same live Celery worker reconnect after Redis restart",
            timeout=45,
        )

        _send_poller(worker.queue)
        _wait_for(
            lambda: all(
                _state(event_id)[0] == "dead_lettered"
                for event_id in (pre_outage, during_outage)
            ),
            description="all PostgreSQL durable work to reach explicit terminal state",
            timeout=45,
        )

        for event_id in (pre_outage, during_outage):
            status, attempts, _max_attempts, fence, leased_by, leased_until, last_error = _state(event_id)
            assert status == "dead_lettered"
            assert attempts == 1
            assert fence == 1
            assert leased_by is None
            assert leased_until is None
            assert last_error

        assert worker.process.poll() is None
        assert worker.process.pid == original_pid

    print("P6B_LIVE_WORKER_RECONNECT=PASS")
    print("P6B_PRE_OUTAGE_DURABLE_WORK=PASS")
    print("P6B_DURING_OUTAGE_DURABLE_WORK=PASS")
    print("P6B_NO_FALSE_SUCCESS=PASS")
