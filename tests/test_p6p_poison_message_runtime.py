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
_DATABASE = "gymflow_p6p_test"
_TIMEOUT = 75.0
_RETRY_WAIT = 45.0


@dataclass(frozen=True)
class _Worker:
    process: subprocess.Popen[str]
    hostname: str
    queue: str
    log: Path


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"P6-P runtime requires {name}")
    return value


def _validate_local_database_url(name: str) -> None:
    parsed = urlparse(_required(name))
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P6-P database host for {name}: {parsed.hostname}")
    if parsed.path.lstrip("/") != _DATABASE:
        raise RuntimeError(f"unsafe P6-P database for {name}: {parsed.path!r}")


def _safe_topology() -> None:
    if os.environ.get("P6P_POISON_FAULTS") != "1":
        raise RuntimeError("P6-P poison faults require explicit CI enablement")
    if os.environ.get("P6P_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P6-P disposable database acknowledgement is absent")
    _validate_local_database_url("TEST_ADMIN_DATABASE_URL")
    _validate_local_database_url("P6P_APP_DATABASE_URL")
    _validate_local_database_url("WORKER_DATABASE_URL")

    broker = urlparse(_required("CELERY_BROKER_URL"))
    if broker.scheme != "rediss" or broker.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("P6-P broker must be disposable local rediss:// Redis")
    if broker.port != 16381:
        raise RuntimeError(f"unsafe P6-P broker port: {broker.port}")


def _connect_url(name: str):
    parsed = urlparse(_required(name))
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def _admin_connect():
    return _connect_url("TEST_ADMIN_DATABASE_URL")


def _app_connect():
    return _connect_url("P6P_APP_DATABASE_URL")


def _worker_connect():
    return _connect_url("WORKER_DATABASE_URL")


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
                    %s,'P6-P Poison Containment Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p6p-{org_id.hex}"),
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
                    %s,%s,'P6-P Poison Containment Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    branch_id,
                    org_id,
                    f"P6P-{branch_id.hex[:8]}",
                    f"p6p-{branch_id.hex}",
                ),
            )
        connection.commit()
    return org_id, branch_id


def _set_app_context(cursor, org_id: uuid.UUID) -> None:
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


def _insert_valid_retrying_poison(
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    with _app_connect() as connection:
        with connection.cursor() as cursor:
            _set_app_context(cursor, org_id)
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object(
                        'branch_id',CAST(%s AS text),
                        'org_id',CAST(%s AS text),
                        'from_status','active',
                        'to_status','temporarily_closed',
                        'actor_id',CAST(%s AS text),
                        'p6p_deterministic_failure',true
                    ),
                    2,%s
                )
                """,
                (
                    event_id,
                    org_id,
                    branch_id,
                    branch_id,
                    org_id,
                    actor_id,
                    uuid.uuid4(),
                ),
            )
        connection.commit()
    return event_id


def _insert_following_malformed_work(
    org_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    with _app_connect() as connection:
        with connection.cursor() as cursor:
            _set_app_context(cursor, org_id)
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object('p6p_following_work','malformed'),3,%s
                )
                """,
                (event_id, org_id, branch_id, uuid.uuid4()),
            )
        connection.commit()
    return event_id


def _state(event_id: uuid.UUID) -> tuple[object, ...]:
    with _worker_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,max_attempts,lease_fence,
                       leased_by,leased_until,last_error,
                       process_after <= pg_catalog.clock_timestamp() AS due
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
    client = _redis_client()
    try:
        return client.ping() is True
    except (redis.RedisError, OSError):
        return False
    finally:
        client.close()


def _queue_depth(queue: str) -> int:
    client = _redis_client()
    try:
        return int(client.llen(queue))
    finally:
        client.close()


def _controller() -> Celery:
    return Celery(
        "p6p-runtime-controller",
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


def _send_task(queue: str, task_name: str) -> None:
    client = _controller()
    try:
        client.send_task(task_name, queue=queue)
    finally:
        client.close()


def _send_poller(queue: str) -> None:
    _send_task(queue, "app.tasks.branch_outbox_poller.run")


def _worker_log(worker: _Worker) -> str:
    try:
        return worker.log.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for forbidden in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "P6P_APP_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "AUTH_RUNTIME_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
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
            "P6P_POISON_FAULTS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


@contextmanager
def _running_worker(tmp_path: Path) -> Iterator[_Worker]:
    token = uuid.uuid4().hex
    hostname = f"p6p-{token}@localhost"
    queue = f"p6p-{token}"
    log = tmp_path / f"p6p-worker-{token}.log"
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
            "--include=scripts.ci.p6p_poison_fault_hooks",
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
                    "P6-P production worker exited during startup:\n"
                    + _worker_log(worker)
                )
            return _worker_ping(hostname)

        _wait_for(ready, description="production Celery worker readiness", timeout=45)
        yield worker
    except Exception as exc:
        raise AssertionError(f"{exc}\nCelery worker log:\n{_worker_log(worker)}") from exc
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


def test_poison_messages_are_contained_without_retry_storm_or_lost_durable_work(
    tmp_path: Path,
) -> None:
    _safe_topology()
    assert _redis_ping() is True
    org_id, branch_id = _seed_branch()
    valid_poison = _insert_valid_retrying_poison(org_id, branch_id)
    following_work = _insert_following_malformed_work(org_id, branch_id)

    with _running_worker(tmp_path) as worker:
        original_pid = worker.process.pid

        unknown_task = f"p6p.unregistered.{uuid.uuid4().hex}"
        _send_task(worker.queue, unknown_task)
        _wait_for(
            lambda: unknown_task in _worker_log(worker)
            and "Received unregistered task" in _worker_log(worker),
            description="unregistered broker message rejection",
            timeout=20,
        )
        _wait_for(
            lambda: _queue_depth(worker.queue) == 0,
            description="unregistered broker message to leave queue",
            timeout=15,
        )
        unknown_occurrences = _worker_log(worker).count(unknown_task)
        assert unknown_occurrences >= 1
        time.sleep(2.0)
        assert _worker_log(worker).count(unknown_task) == unknown_occurrences
        assert _queue_depth(worker.queue) == 0
        assert worker.process.poll() is None
        assert worker.process.pid == original_pid
        assert _worker_ping(worker.hostname) is True

        _send_poller(worker.queue)
        _wait_for(
            lambda: _state(valid_poison)[0] == "pending"
            and _state(valid_poison)[1] == 1
            and _state(following_work)[0] == "dead_lettered",
            description="first valid poison retry and following durable disposition",
            timeout=30,
        )

        first_state = _state(valid_poison)
        assert first_state[0] == "pending"
        assert first_state[1] == 1
        assert first_state[2] == 2
        assert first_state[3] == 1
        assert first_state[4] is None
        assert first_state[5] is None
        assert "P6-P deterministic valid-command failure" in str(first_state[6])

        following_state = _state(following_work)
        assert following_state[0] == "dead_lettered"
        assert following_state[1] == 1
        assert following_state[3] == 1
        assert following_state[4] is None
        assert following_state[5] is None
        assert following_state[6]

        _wait_for(
            lambda: bool(_state(valid_poison)[7]),
            description="bounded production retry backoff to expire",
            timeout=_RETRY_WAIT,
        )
        _send_poller(worker.queue)
        _wait_for(
            lambda: _state(valid_poison)[0] == "dead_lettered"
            and _state(valid_poison)[1] == 2,
            description="valid durable poison to exhaust bounded retries",
            timeout=30,
        )

        terminal_valid = _state(valid_poison)
        terminal_following = _state(following_work)
        assert terminal_valid[0] == "dead_lettered"
        assert terminal_valid[1] == 2
        assert terminal_valid[2] == 2
        assert terminal_valid[3] == 2
        assert terminal_valid[4] is None
        assert terminal_valid[5] is None
        assert "P6-P deterministic valid-command failure" in str(terminal_valid[6])

        for _ in range(3):
            _send_poller(worker.queue)
            _wait_for(
                lambda: _queue_depth(worker.queue) == 0,
                description="later poll cycle to drain",
                timeout=15,
            )
            time.sleep(0.5)

        assert _state(valid_poison) == terminal_valid
        assert _state(following_work) == terminal_following
        assert worker.process.poll() is None
        assert worker.process.pid == original_pid
        assert _worker_ping(worker.hostname) is True

    print("P6P_UNREGISTERED_BROKER_MESSAGE_DISCARDED=PASS")
    print("P6P_WORKER_FLEET_SURVIVES_POISON=PASS")
    print("P6P_VALID_DURABLE_POISON_RETRY_BOUNDED=PASS")
    print("P6P_TERMINAL_POISON_NOT_REDISPATCHED=PASS")
    print("P6P_BATCH_PROGRESS_AFTER_POISON=PASS")
    print("P6P_NO_FALSE_SUCCESS=PASS")
