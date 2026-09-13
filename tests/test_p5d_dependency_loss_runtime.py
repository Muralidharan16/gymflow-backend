from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
import pytest
import redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]
_DATABASE = "gymflow_p5d_test"
_ADMIN_LOGIN = "migration_owner"
_AUTH_LOGIN = "auth_p5d_runtime"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_TIMEOUT = 60.0


@dataclass(frozen=True)
class _BaseSeed:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    branch_id: uuid.UUID


@dataclass(frozen=True)
class _CeleryWorker:
    process: subprocess.Popen[str]
    hostname: str
    queue: str
    telemetry: Path
    release: Path
    log: Path


def _required_url(name: str):
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"P5-D runtime requires {name}")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-D {name} must include host, port and database")
    return url


def _safe_database_topology() -> tuple[str, int, str]:
    if os.environ.get("P5D_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-D destructive faults require explicit CI enablement")
    runtime = _required_url("TEST_DATABASE_URL")
    admin = _required_url("TEST_ADMIN_DATABASE_URL")
    runtime_topology = (str(runtime.host), int(runtime.port), str(runtime.database))
    admin_topology = (str(admin.host), int(admin.port), str(admin.database))
    if runtime_topology != admin_topology:
        raise RuntimeError("P5-D runtime/admin URLs must target one disposable topology")
    host, _port, database = runtime_topology
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-D database host: {host}")
    if database != _DATABASE:
        raise RuntimeError(f"unsafe P5-D database: {database}")
    if os.environ.get("P5D_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P5-D disposable database acknowledgement is absent")
    return runtime_topology


def _connect(login: str, password_environment: str):
    host, port, database = _safe_database_topology()
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=login,
        password=os.environ[password_environment],
    )


def _admin_psql(sql: str, *, tuples_only: bool = False) -> str:
    _safe_database_topology()
    command = [
        "sudo",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        _DATABASE,
    ]
    if tuples_only:
        command.extend(["-A", "-t"])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-D CI infrastructure SQL failed:\n{completed.stdout}")
    return completed.stdout.strip()


def _wait_for(
    predicate: Callable[[], Any],
    *,
    description: str,
    timeout: float = _TIMEOUT,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def _seed_base(*, companion_operational_branch: bool = False) -> _BaseSeed:
    seed = _BaseSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    companion_id = uuid.uuid4() if companion_operational_branch else None
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P5-D Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (seed.org_id, f"p5d-runtime-{seed.org_id.hex}"),
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(seed.org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.owners(
                    id,org_id,owner_name,email,hashed_password,
                    email_verified,onboarding_completed
                ) VALUES (
                    %s,%s,'P5-D Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (seed.owner_id, seed.org_id, f"p5d-{seed.owner_id.hex}@example.test"),
            )
            branch_rows = [(seed.branch_id, "Primary")]
            if companion_id is not None:
                branch_rows.append((companion_id, "Companion"))
            for branch_id, label in branch_rows:
                cursor.execute(
                    """
                    INSERT INTO public.org_branches(
                        id,org_id,branch_name,branch_code,internal_slug,
                        timezone,currency_code,region_code,country_code
                    ) VALUES (
                        %s,%s,%s,%s,%s,'Asia/Kolkata','INR','TN','IN'
                    )
                    """,
                    (
                        branch_id,
                        seed.org_id,
                        f"P5-D {label}",
                        f"P5D-{branch_id.hex[:8]}",
                        f"p5d-{branch_id.hex}",
                    ),
                )
        connection.commit()

    _insert_initial_branch_state(seed, seed.branch_id, is_primary=True)
    if companion_id is not None:
        _insert_initial_branch_state(seed, companion_id, is_primary=False)
    return seed


def _insert_initial_branch_state(
    seed: _BaseSeed,
    branch_id: uuid.UUID,
    *,
    is_primary: bool,
) -> None:
    with _connect(_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true),
                    pg_catalog.set_config('app.current_gym_id','',true)
                """,
                (str(seed.org_id), str(seed.owner_id)),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branch_state(
                    branch_id,org_id,branch_status,is_primary,is_active,
                    is_public,status,is_operational,status_changed_by,
                    status_reason,transition_source,scheduled_transition_at,
                    scheduled_transition_to,lifecycle_transition_in_progress,
                    saga_last_checkpoint,saga_compensation_strategy,
                    watchdog_recovered_at,watchdog_recovery_count,
                    search_visibility_version,search_last_synced_at,
                    search_sync_failed_at,reconciliation_claimed_by,
                    reconciliation_claimed_at,worm_archive_uri,
                    worm_archive_checksum,worm_archive_verified_at,
                    worm_archive_status,version,search_logical_clock,
                    search_epoch_ulid,deleted_at,archived_at,purged_at
                ) VALUES (
                    %s,%s,'active',%s,true,true,'active',true,NULL,NULL,
                    'api',NULL,NULL,false,NULL,NULL,NULL,0,1,NULL,NULL,NULL,
                    NULL,NULL,NULL,NULL,NULL,1,0,%s,NULL,NULL,NULL
                )
                """,
                (
                    branch_id,
                    seed.org_id,
                    is_primary,
                    uuid.uuid4().hex[:26].upper(),
                ),
            )
        connection.commit()


def _insert_malformed_lifecycle(seed: _BaseSeed, fault_label: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as connection:
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
                (str(seed.org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.lifecycle_saga',
                    jsonb_build_object('p5d_fault',CAST(%s AS text)),3,%s
                )
                """,
                (event_id, seed.org_id, seed.branch_id, fault_label, uuid.uuid4()),
            )
        connection.commit()
    return event_id


def _insert_search_event(seed: _BaseSeed) -> uuid.UUID:
    event_id = uuid.uuid4()
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as connection:
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
                (str(seed.org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (%s,%s,%s,'branch.search_index','{}'::jsonb,5,%s)
                """,
                (event_id, seed.org_id, seed.branch_id, uuid.uuid4()),
            )
        connection.commit()
    return event_id


def _outbox_state(event_id: uuid.UUID) -> tuple[Any, ...]:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,max_attempts,lease_fence,
                       leased_by,leased_until,process_after,last_error
                FROM public.branch_outbox_events
                WHERE outbox_id=%s
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            return row


def _search_state(seed: _BaseSeed) -> tuple[Any, ...]:
    with _connect(_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true)
                """,
                (str(seed.org_id), str(seed.owner_id)),
            )
            cursor.execute(
                """
                SELECT search_visibility_version,search_provider_ack_version,
                       search_last_synced_at,search_sync_failed_at,
                       lifecycle_transition_in_progress,saga_last_checkpoint,status
                FROM public.org_branch_state
                WHERE branch_id=%s AND org_id=%s
                """,
                (seed.branch_id, seed.org_id),
            )
            row = cursor.fetchone()
            assert row is not None
            return row


def _accelerate(event_id: uuid.UUID) -> None:
    output = _admin_psql(
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp(), "
        "leased_until=CASE WHEN status='processing' "
        "THEN pg_catalog.clock_timestamp()-INTERVAL '1 second' ELSE leased_until END "
        f"WHERE outbox_id='{event_id}'::uuid;"
    )
    assert "UPDATE 1" in output.splitlines()


def _run_replacement_worker(*, provider_url: str | None = None) -> dict[str, int]:
    environment = os.environ.copy()
    environment["ENVIRONMENT"] = "test"
    if provider_url:
        environment.update(
            {
                "SEARCH_PROVIDER_MODE": "opensearch",
                "OPENSEARCH_URL": provider_url,
                "OPENSEARCH_INDEX": "branches-v1",
                "OPENSEARCH_USERNAME": "",
                "OPENSEARCH_PASSWORD": "",
                "OPENSEARCH_TIMEOUT_SECONDS": "1",
                "OPENSEARCH_VERIFY_TLS": "false",
            }
        )
    else:
        environment.update(
            {
                "SEARCH_PROVIDER_MODE": "disabled",
                "OPENSEARCH_URL": "",
                "OPENSEARCH_USERNAME": "",
                "OPENSEARCH_PASSWORD": "",
            }
        )
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.ci.p5d_fault_worker"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-D replacement worker failed:\n{completed.stdout}")
    marker = next(
        (line for line in completed.stdout.splitlines() if line.startswith("P5D_WORKER_RESULT=")),
        None,
    )
    if marker is None:
        raise AssertionError(f"P5-D replacement worker emitted no result:\n{completed.stdout}")
    return json.loads(marker.split("=", 1)[1])


def _start_fault_worker(*, provider_url: str | None = None) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["ENVIRONMENT"] = "test"
    if provider_url:
        environment.update(
            {
                "SEARCH_PROVIDER_MODE": "opensearch",
                "OPENSEARCH_URL": provider_url,
                "OPENSEARCH_INDEX": "branches-v1",
                "OPENSEARCH_TIMEOUT_SECONDS": "1",
                "OPENSEARCH_VERIFY_TLS": "false",
            }
        )
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.ci.p5d_fault_worker"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _finish_fault_worker(process: subprocess.Popen[str], *, expect_success: bool) -> str:
    output, _ = process.communicate(timeout=45)
    if expect_success and process.returncode != 0:
        raise AssertionError(f"P5-D worker unexpectedly failed:\n{output}")
    if not expect_success and process.returncode == 0:
        raise AssertionError(f"P5-D worker unexpectedly succeeded:\n{output}")
    return output


def _install_claim_barrier(event_id: uuid.UUID) -> None:
    _drop_barriers()
    _admin_psql(
        f"""
        CREATE FUNCTION public.p5d_claim_barrier() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status='pending' AND NEW.status='processing'
             AND NEW.outbox_id='{event_id}'::uuid THEN
            PERFORM pg_catalog.pg_sleep(10);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER p5d_claim_barrier
          BEFORE UPDATE ON public.branch_outbox_events
          FOR EACH ROW EXECUTE FUNCTION public.p5d_claim_barrier();
        """
    )


def _install_mutation_barrier(branch_id: uuid.UUID) -> None:
    _drop_barriers()
    _admin_psql(
        f"""
        CREATE FUNCTION public.p5d_mutation_barrier() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.event_type='transaction_b_started'
             AND NEW.branch_id='{branch_id}'::uuid THEN
            PERFORM pg_catalog.pg_sleep(10);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER p5d_mutation_barrier
          BEFORE INSERT ON public.branch_lifecycle_events
          FOR EACH ROW EXECUTE FUNCTION public.p5d_mutation_barrier();
        """
    )


def _install_ack_barrier(branch_id: uuid.UUID) -> None:
    _drop_barriers()
    _admin_psql(
        f"""
        CREATE FUNCTION public.p5d_ack_barrier() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.branch_id='{branch_id}'::uuid
             AND NEW.search_provider_ack_version IS DISTINCT FROM OLD.search_provider_ack_version THEN
            PERFORM pg_catalog.pg_sleep(10);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER p5d_ack_barrier
          BEFORE UPDATE OF search_provider_ack_version ON public.org_branch_state
          FOR EACH ROW EXECUTE FUNCTION public.p5d_ack_barrier();
        """
    )


def _drop_barriers() -> None:
    _admin_psql(
        """
        DROP TRIGGER IF EXISTS p5d_claim_barrier ON public.branch_outbox_events;
        DROP TRIGGER IF EXISTS p5d_mutation_barrier ON public.branch_lifecycle_events;
        DROP TRIGGER IF EXISTS p5d_ack_barrier ON public.org_branch_state;
        DROP FUNCTION IF EXISTS public.p5d_claim_barrier();
        DROP FUNCTION IF EXISTS public.p5d_mutation_barrier();
        DROP FUNCTION IF EXISTS public.p5d_ack_barrier();
        """
    )


def _wait_for_sleeping_worker(query_fragment: str) -> int:
    escaped = query_fragment.replace("'", "''")

    def probe() -> int | None:
        output = _admin_psql(
            "SELECT pid FROM pg_catalog.pg_stat_activity "
            "WHERE usename='worker_test_runtime' "
            "AND state='active' AND wait_event='PgSleep' "
            f"AND query ILIKE '%{escaped}%' ORDER BY pid LIMIT 1;",
            tuples_only=True,
        )
        return int(output) if output.strip().isdigit() else None

    return int(_wait_for(probe, description=f"worker boundary {query_fragment!r}", timeout=12))


def _terminate_backend(pid: int) -> None:
    output = _admin_psql(
        f"SELECT pg_catalog.pg_terminate_backend({int(pid)});",
        tuples_only=True,
    )
    assert output.splitlines()[-1].strip() == "t"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _provider_effect_count(state_path: Path) -> int:
    value = json.loads(state_path.read_text(encoding="utf-8"))
    return int(value["mutation_count"])


def _start_provider(state_path: Path, port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.ci.p5d_fake_opensearch",
            "--port",
            str(port),
            "--state",
            str(state_path),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def healthy() -> bool:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"P5-D provider exited during startup:\n{output}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    _wait_for(healthy, description="P5-D provider readiness", timeout=10)
    return process


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _redis_container_id() -> str:
    completed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "ancestor=redis:7-alpine"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(ids) != 1:
        raise AssertionError(f"P5-D expected exactly one disposable Redis container: {completed.stdout}")
    return ids[0]


def _redis_ping() -> bool:
    try:
        client = redis.Redis.from_url(
            os.environ["CELERY_BROKER_URL"],
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return client.ping() is True
    except redis.RedisError:
        return False


def _stop_redis() -> None:
    container_id = _redis_container_id()
    completed = subprocess.run(
        ["docker", "stop", "-t", "1", container_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-D failed to stop Redis:\n{completed.stdout}")
    _wait_for(lambda: not _redis_ping(), description="Redis outage", timeout=8)


def _start_redis() -> None:
    container_id = _redis_container_id()
    completed = subprocess.run(
        ["docker", "start", container_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-D failed to restart Redis:\n{completed.stdout}")
    _wait_for(_redis_ping, description="Redis recovery", timeout=15)


def _telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


@contextmanager
def _running_celery_worker(
    tmp_path: Path,
    *,
    target_event_id: uuid.UUID | None = None,
    broker_fault_mode: str = "",
) -> Iterator[_CeleryWorker]:
    token = uuid.uuid4().hex
    hostname = f"p5d-{token}@localhost"
    queue = f"p5d-{token}"
    telemetry = tmp_path / f"telemetry-{token}.jsonl"
    release = tmp_path / f"release-{token}.sentinel"
    log = tmp_path / f"worker-{token}.log"
    environment = os.environ.copy()
    for forbidden in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "AUTH_RUNTIME_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
    ):
        environment[forbidden] = ""
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
            "P5D_TELEMETRY_PATH": str(telemetry),
            "P5D_RELEASE_PATH": str(release),
            "P5D_TARGET_EVENT_ID": str(target_event_id or ""),
            "P5D_BROKER_FAULT_MODE": broker_fault_mode,
            "PYTHONUNBUFFERED": "1",
        }
    )
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
            "--without-heartbeat",
            "--loglevel=INFO",
            "--include=scripts.ci.p5d_celery_fault_hooks",
        ],
        cwd=ROOT,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    worker = _CeleryWorker(process, hostname, queue, telemetry, release, log)
    try:
        from app.core.celery_app import celery_app

        def ready() -> bool:
            if process.poll() is not None:
                raise AssertionError(f"P5-D Celery worker exited:\n{log.read_text(encoding='utf-8')}")
            replies = celery_app.control.ping(destination=[hostname], timeout=1.0)
            return any(reply.get(hostname, {}).get("ok") == "pong" for reply in replies)

        _wait_for(ready, description="P5-D Celery worker readiness", timeout=30)
        yield worker
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()


def _send_poller_task(queue: str) -> None:
    from app.core.celery_app import celery_app

    celery_app.send_task("app.tasks.branch_outbox_poller.run", queue=queue)


def _provider_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


async def _initiate_temporary_close(seed: _BaseSeed) -> uuid.UUID:
    from app.core.database import update_session_context
    from app.services.branch_lifecycle_service import BranchLifecycleService

    source = _required_url("TEST_DATABASE_URL")
    async_url = source.set(drivername="postgresql+asyncpg")
    engine = create_async_engine(async_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            await update_session_context(
                session,
                principal_id=str(seed.owner_id),
                principal_type="owner",
                org_id=str(seed.org_id),
                trace_id=str(uuid.uuid4()),
                role="owner",
            )
            service = BranchLifecycleService(session)
            return await service.initiate_transition(
                branch_id=seed.branch_id,
                org_id=seed.org_id,
                to_status="temporarily_closed",
                actor_id=seed.owner_id,
                actor_role="owner",
                transition_source="p5d_runtime",
            )
    finally:
        await engine.dispose()


def _correlated_event(correlation_id: uuid.UUID, event_type: str) -> uuid.UUID:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT outbox_id
                FROM public.branch_outbox_events
                WHERE correlation_id=%s AND event_type=%s
                ORDER BY created_at,outbox_id
                """,
                (correlation_id, event_type),
            )
            rows = cursor.fetchall()
            assert len(rows) == 1
            return rows[0][0]


def _postpone_correlated_search(correlation_id: uuid.UUID) -> None:
    output = _admin_psql(
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp()+INTERVAL '1 hour' "
        f"WHERE correlation_id='{correlation_id}'::uuid "
        "AND event_type IN ('branch.search_index','branch.search_deindex');"
    )
    assert "UPDATE 1" in output.splitlines()


def _child_count(correlation_id: uuid.UUID, event_type: str) -> int:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM public.branch_outbox_events WHERE correlation_id=%s AND event_type=%s",
                (correlation_id, event_type),
            )
            return int(cursor.fetchone()[0])


def test_redis_loss_before_delivery_recovers_from_postgresql(tmp_path: Path) -> None:
    _safe_database_topology()
    seed = _seed_base()
    event_id = _insert_malformed_lifecycle(seed, "redis-before-delivery")
    assert _redis_ping() is True
    _stop_redis()
    try:
        assert _redis_ping() is False
        status, attempts, _max_attempts, fence, leased_by, *_ = _outbox_state(event_id)
        assert (status, attempts, fence, leased_by) == ("pending", 0, 0, None)
    finally:
        _start_redis()

    with _running_celery_worker(tmp_path) as worker:
        _send_poller_task(worker.queue)
        _wait_for(
            lambda: _outbox_state(event_id)[0] == "dead_lettered",
            description="PostgreSQL durable job after Redis restart",
        )
    status, attempts, _max_attempts, fence, leased_by, leased_until, *_ = _outbox_state(event_id)
    assert (status, attempts, fence, leased_by, leased_until) == (
        "dead_lettered",
        1,
        1,
        None,
        None,
    )


def test_redis_loss_after_db_commit_before_task_ack_keeps_postgresql_authority(
    tmp_path: Path,
) -> None:
    _safe_database_topology()
    seed = _seed_base()
    event_id = _insert_malformed_lifecycle(seed, "redis-before-ack")
    assert _redis_ping() is True
    try:
        with _running_celery_worker(
            tmp_path,
            target_event_id=event_id,
            broker_fault_mode="after_db_commit_before_task_ack",
        ) as worker:
            _send_poller_task(worker.queue)
            _wait_for(
                lambda: any(
                    record.get("event") == "after_db_commit_before_task_ack"
                    and record.get("event_id") == str(event_id)
                    for record in _telemetry(worker.telemetry)
                ),
                description="post-commit pre-ack barrier",
            )
            assert _outbox_state(event_id)[0] == "dead_lettered"
            _stop_redis()
            worker.release.write_text("release\n", encoding="utf-8")
            time.sleep(1.0)
    finally:
        if not _redis_ping():
            _start_redis()

    before = _outbox_state(event_id)
    with _running_celery_worker(tmp_path) as replacement:
        _send_poller_task(replacement.queue)
        time.sleep(1.0)
    after = _outbox_state(event_id)
    assert before[:6] == after[:6]
    assert after[0] == "dead_lettered"
    assert after[1] == 1


def test_provider_network_loss_requeues_and_replacement_process_recovers(tmp_path: Path) -> None:
    _safe_database_topology()
    seed = _seed_base()
    event_id = _insert_search_event(seed)
    state_path = tmp_path / "provider-network.json"
    port = _free_port()
    provider = _start_provider(state_path, port)
    _stop_process(provider)

    first = _run_replacement_worker(provider_url=_provider_url(port))
    assert first["retry"] == 1
    status, attempts, max_attempts, _fence, leased_by, leased_until, *_ = _outbox_state(event_id)
    assert status == "pending"
    assert attempts == 1 and attempts < max_attempts
    assert leased_by is None and leased_until is None
    search_state = _search_state(seed)
    assert search_state[1] is None
    assert search_state[2] is None
    assert search_state[3] is not None

    provider = _start_provider(state_path, port)
    try:
        _accelerate(event_id)
        second = _run_replacement_worker(provider_url=_provider_url(port))
        assert second["delivered"] == 1
        assert _outbox_state(event_id)[0] == "delivered"
        search_state = _search_state(seed)
        assert int(search_state[1]) == 1
        assert search_state[2] is not None
        assert _provider_effect_count(state_path) == 1
    finally:
        _stop_process(provider)


def test_database_disconnect_at_claim_rolls_back_and_recovers() -> None:
    _safe_database_topology()
    seed = _seed_base()
    event_id = _insert_malformed_lifecycle(seed, "pending_to_processing")
    _install_claim_barrier(event_id)
    process = _start_fault_worker()
    try:
        pid = _wait_for_sleeping_worker("UPDATE public.branch_outbox_events AS outbox_data")
        _terminate_backend(pid)
        _finish_fault_worker(process, expect_success=False)
    finally:
        _drop_barriers()
        if process.poll() is None:
            _stop_process(process)

    status, attempts, _max_attempts, fence, leased_by, leased_until, *_ = _outbox_state(event_id)
    assert (status, attempts, fence, leased_by, leased_until) == (
        "pending",
        0,
        0,
        None,
        None,
    )
    replacement = _run_replacement_worker()
    assert replacement["dead_lettered"] == 1
    status, attempts, _max_attempts, fence, leased_by, leased_until, *_ = _outbox_state(event_id)
    assert (status, attempts, fence, leased_by, leased_until) == (
        "dead_lettered",
        1,
        1,
        None,
        None,
    )


def test_database_disconnect_during_domain_mutation_rolls_back_and_recovers() -> None:
    _safe_database_topology()
    seed = _seed_base(companion_operational_branch=True)
    correlation_id = asyncio.run(_initiate_temporary_close(seed))
    parent_id = _correlated_event(correlation_id, "branch.lifecycle_saga")
    _postpone_correlated_search(correlation_id)
    initial_state = _search_state(seed)
    assert initial_state[4] is True
    assert initial_state[5] is None
    assert initial_state[6] == "temporarily_closed"

    _install_mutation_barrier(seed.branch_id)
    process = _start_fault_worker()
    try:
        pid = _wait_for_sleeping_worker("branch_lifecycle_events")
        _terminate_backend(pid)
        _finish_fault_worker(process, expect_success=True)
    finally:
        _drop_barriers()
        if process.poll() is None:
            _stop_process(process)

    parent = _outbox_state(parent_id)
    assert parent[0] == "pending"
    assert parent[1] == 1
    after_kill = _search_state(seed)
    assert after_kill[4] is True
    assert after_kill[5] is None
    assert after_kill[6] == "temporarily_closed"
    assert _child_count(correlation_id, "branch.refund_required") == 0
    assert _child_count(correlation_id, "branch.member_notification") == 0

    _accelerate(parent_id)
    replacement = _run_replacement_worker()
    assert replacement["delivered"] == 1
    final_state = _search_state(seed)
    assert final_state[4] is False
    assert final_state[5] is None
    assert final_state[6] == "temporarily_closed"
    assert _outbox_state(parent_id)[0] == "delivered"
    assert _child_count(correlation_id, "branch.refund_required") == 1
    assert _child_count(correlation_id, "branch.member_notification") == 1


def test_database_disconnect_during_provider_ack_rebinds_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    _safe_database_topology()
    seed = _seed_base()
    event_id = _insert_search_event(seed)
    state_path = tmp_path / "provider-ack.json"
    port = _free_port()
    provider = _start_provider(state_path, port)
    _install_ack_barrier(seed.branch_id)
    process = _start_fault_worker(provider_url=_provider_url(port))
    try:
        pid = _wait_for_sleeping_worker("acknowledge_branch_search_effect")
        _terminate_backend(pid)
        output = _finish_fault_worker(process, expect_success=True)
        assert "P5D_WORKER_RESULT=" in output
    finally:
        _drop_barriers()
        if process.poll() is None:
            _stop_process(process)

    try:
        assert _provider_effect_count(state_path) == 1
        state_after_kill = _search_state(seed)
        assert state_after_kill[1] is None
        assert state_after_kill[2] is None
        outbox = _outbox_state(event_id)
        assert outbox[0] == "pending"
        assert outbox[1] == 1
        assert outbox[4] is None and outbox[5] is None

        _accelerate(event_id)
        replacement = _run_replacement_worker(provider_url=_provider_url(port))
        assert replacement["delivered"] == 1
        final_state = _search_state(seed)
        assert int(final_state[1]) == 1
        assert final_state[2] is not None
        assert _outbox_state(event_id)[0] == "delivered"
        assert _provider_effect_count(state_path) == 1
    finally:
        _stop_process(provider)
