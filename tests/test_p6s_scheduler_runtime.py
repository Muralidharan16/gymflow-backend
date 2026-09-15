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


ROOT = Path(__file__).resolve().parents[1]
_DATABASE = "gymflow_p6s_test"
_BEAT_SEND_MARKER = "Scheduler: Sending due task poll-branch-outbox"
_TIMEOUT = 150.0


@dataclass(frozen=True)
class _Process:
    process: subprocess.Popen[str]
    log: Path


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"P6-S runtime requires {name}")
    return value


def _wait_for(predicate: Callable[[], bool], *, description: str, timeout: float = _TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {description}")


def _validate_topology() -> None:
    if os.environ.get("P6S_SCHEDULER_FAULTS") != "1":
        raise RuntimeError("P6-S runtime requires explicit disposable-test enablement")
    if os.environ.get("P6S_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P6-S disposable database acknowledgement is absent")
    for name in ("TEST_ADMIN_DATABASE_URL", "P6S_APP_DATABASE_URL", "WORKER_DATABASE_URL"):
        parsed = urlparse(_required(name))
        if parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise RuntimeError(f"unsafe P6-S database host for {name}")
        if parsed.path.lstrip("/") != _DATABASE:
            raise RuntimeError(f"unsafe P6-S database name for {name}")
    for name in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        parsed = urlparse(_required(name))
        if parsed.scheme != "rediss" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise RuntimeError(f"P6-S {name} must be local rediss://")
        if parsed.port != 16382:
            raise RuntimeError(f"unsafe P6-S Redis port for {name}")


def _connect(name: str):
    parsed = urlparse(_required(name))
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def _redis(name: str) -> redis.Redis:
    return redis.Redis.from_url(
        _required(name),
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )


def _read_log(runtime: _Process) -> str:
    try:
        return runtime.log.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _beat_send_count(runtime: _Process) -> int:
    return _read_log(runtime).count(_BEAT_SEND_MARKER)


def _beat_env(owner_id: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "WORKER_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "P6S_APP_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
    ):
        env.pop(key, None)
    env.update(
        {
            "ENVIRONMENT": "production",
            "DOERS_PROCESS_PROFILE": "beat",
            "CELERY_WORKER_PROFILE": "",
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
            "P6S_SCHEDULER_FAULTS": "1",
            "P6S_TEST_BEAT_OWNER_ID": owner_id,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "P6S_APP_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
        "P6S_TEST_BEAT_OWNER_ID",
    ):
        env.pop(key, None)
    env.update(
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
    return env


@contextmanager
def _running_beat(tmp_path: Path, owner_id: str, *, bypass_owner: bool = False) -> Iterator[_Process]:
    token = uuid.uuid4().hex
    log = tmp_path / f"p6s-beat-{owner_id}-{token}.log"
    schedule = tmp_path / f"p6s-beat-{owner_id}-{token}.schedule"
    handle = log.open("w", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.tasks.celery_app",
        "beat",
        "--loglevel=INFO",
        f"--schedule={schedule}",
    ]
    if bypass_owner:
        command.append("--scheduler=celery.beat:PersistentScheduler")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=_beat_env(owner_id),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    runtime = _Process(process=process, log=log)
    try:
        def ready() -> bool:
            if process.poll() is not None:
                handle.flush()
                raise AssertionError("Beat exited during startup:\n" + _read_log(runtime))
            handle.flush()
            return "beat: Starting..." in _read_log(runtime)

        _wait_for(ready, description=f"Beat {owner_id} startup", timeout=30)
        yield runtime
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


@contextmanager
def _running_worker(tmp_path: Path) -> Iterator[_Process]:
    token = uuid.uuid4().hex
    log = tmp_path / f"p6s-worker-{token}.log"
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=2",
            "--prefetch-multiplier=1",
            "--queues=worker",
            "--without-gossip",
            "--without-mingle",
            "--loglevel=INFO",
        ],
        cwd=ROOT,
        env=_worker_env(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    runtime = _Process(process=process, log=log)
    try:
        def ready() -> bool:
            if process.poll() is not None:
                handle.flush()
                raise AssertionError("Worker exited during startup:\n" + _read_log(runtime))
            handle.flush()
            return " ready." in _read_log(runtime)

        _wait_for(ready, description="worker readiness", timeout=45)
        yield runtime
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


def _queue_depth(queue: str) -> int:
    client = _redis("CELERY_BROKER_URL")
    try:
        return int(client.llen(queue))
    finally:
        client.close()


def _clear_delivery_queues() -> None:
    client = _redis("CELERY_BROKER_URL")
    try:
        client.delete("worker", "lifecycle-maintenance")
    finally:
        client.close()


def _seed_durable_obligation() -> uuid.UUID:
    org_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    event_id = uuid.uuid4()
    with _connect("TEST_ADMIN_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P6-S Scheduler Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p6s-{org_id.hex}"),
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
                    %s,%s,'P6-S Scheduler Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (branch_id, org_id, f"P6S-{branch_id.hex[:8]}", f"p6s-{branch_id.hex}"),
            )
        connection.commit()

    with _connect("P6S_APP_DATABASE_URL") as connection:
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
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object('p6s_duplicate_scheduler_probe','malformed'),3,%s
                )
                """,
                (event_id, org_id, branch_id, uuid.uuid4()),
            )
        connection.commit()
    return event_id


def _state(event_id: uuid.UUID) -> tuple[object, ...]:
    with _connect("WORKER_DATABASE_URL") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,max_attempts,lease_fence,leased_by,leased_until,last_error
                FROM public.branch_outbox_events
                WHERE outbox_id=%s
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            return row


def test_real_beat_contenders_enforce_renewable_single_owner(tmp_path: Path) -> None:
    _validate_topology()
    _clear_delivery_queues()
    key = _required("CELERY_BEAT_OWNERSHIP_KEY")
    ttl = float(_required("CELERY_BEAT_OWNERSHIP_TTL_SECONDS"))
    owner_a = f"p6s-a-{uuid.uuid4().hex}"
    owner_b = f"p6s-b-{uuid.uuid4().hex}"
    ownership = _redis("REDIS_URL")
    ownership.delete(key)
    try:
        with _running_beat(tmp_path, owner_a) as beat_a, _running_beat(tmp_path, owner_b) as beat_b:
            _wait_for(
                lambda: ownership.get(key) in {owner_a, owner_b},
                description="one Beat owner token",
                timeout=20,
            )
            current = ownership.get(key)
            assert current in {owner_a, owner_b}
            if current == owner_a:
                active_id, active, standby_id, standby = owner_a, beat_a, owner_b, beat_b
            else:
                active_id, active, standby_id, standby = owner_b, beat_b, owner_a, beat_a

            _wait_for(
                lambda: _beat_send_count(active) >= 1,
                description="active owner periodic publication",
                timeout=90,
            )
            assert _beat_send_count(standby) == 0
            assert active.process.poll() is None
            assert standby.process.poll() is None
            print("P6S_BEAT_DATABASE_CREDENTIALS_ABSENT=PASS")
            print("P6S_SINGLE_ACTIVE_BEAT=PASS")

            ownership.set(key, standby_id, px=max(1, int(ttl * 1000)))
            active_before = _beat_send_count(active)
            standby_before = _beat_send_count(standby)
            _wait_for(
                lambda: _beat_send_count(standby) > standby_before,
                description="standby publication after ownership transfer",
                timeout=90,
            )
            time.sleep(3.0)
            assert _beat_send_count(active) == active_before
            assert active.process.poll() is None
            assert standby.process.poll() is None
            assert ownership.get(key) == standby_id
            print("P6S_STALE_OWNER_STOPS_PUBLISHING=PASS")

            os.killpg(standby.process.pid, signal.SIGKILL)
            standby.process.wait(timeout=5)
            active_recovery_before = _beat_send_count(active)
            _wait_for(
                lambda: ownership.get(key) == active_id,
                description="surviving contender lease reacquisition",
                timeout=ttl + 15,
            )
            _wait_for(
                lambda: _beat_send_count(active) > active_recovery_before,
                description="recovered owner periodic publication",
                timeout=90,
            )
            assert active.process.poll() is None
            print("P6S_OWNERSHIP_RECOVERY_AFTER_OWNER_DEATH=PASS")
    finally:
        ownership.delete(key)
        ownership.close()
        _clear_delivery_queues()


def test_duplicate_scheduler_publication_converges_on_one_postgresql_effect(tmp_path: Path) -> None:
    _validate_topology()
    _clear_delivery_queues()
    event_id = _seed_durable_obligation()

    with _running_beat(tmp_path, "dup-a", bypass_owner=True) as beat_a, _running_beat(
        tmp_path, "dup-b", bypass_owner=True
    ) as beat_b:
        _wait_for(
            lambda: _beat_send_count(beat_a) >= 1 and _beat_send_count(beat_b) >= 1,
            description="both deliberately unprotected Beats to publish same periodic trigger",
            timeout=90,
        )
        assert _queue_depth("worker") >= 2
        print("P6S_FORCED_DUPLICATE_PERIODIC_PUBLICATION=PASS")

    queued_before_worker = _queue_depth("worker")
    assert queued_before_worker >= 2

    with _running_worker(tmp_path) as worker:
        _wait_for(
            lambda: _state(event_id)[0] == "dead_lettered",
            description="durable obligation terminal disposition",
            timeout=30,
        )
        _wait_for(
            lambda: _queue_depth("worker") == 0,
            description="duplicate periodic delivery drain",
            timeout=45,
        )
        terminal = _state(event_id)
        assert terminal[0] == "dead_lettered"
        assert terminal[1] == 1
        assert terminal[2] == 3
        assert terminal[3] == 1
        assert terminal[4] is None
        assert terminal[5] is None
        assert terminal[6]
        time.sleep(2.0)
        assert _state(event_id) == terminal
        assert worker.process.poll() is None
        assert _read_log(worker).count("app.tasks.branch_outbox_poller.run") >= 2
        print("P6S_DUPLICATE_WORKER_DELIVERY=PASS")
        print("P6S_SINGLE_AUTHORITATIVE_EFFECT=PASS")
        print("P6S_DURABLE_TERMINAL_STATE=PASS")

    _clear_delivery_queues()
