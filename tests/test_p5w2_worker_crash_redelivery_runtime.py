from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import psycopg
import pytest
import redis
from psycopg.errors import InsufficientPrivilege
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
_ADMIN_LOGIN = "migration_owner"
_AUTH_LOGIN = "auth_p5w2_runtime"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_DATABASE = "gymflow_p5w2_test"
_TASK_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _Surface:
    name: str
    task_name: str
    relation: str
    id_column: str
    success_key: str


@dataclass(frozen=True)
class _Seed:
    surface: _Surface
    org_id: uuid.UUID
    owner_id: uuid.UUID
    branch_id: uuid.UUID
    event_id: uuid.UUID


@dataclass(frozen=True)
class _Worker:
    process: subprocess.Popen[str]
    hostname: str
    queue: str
    telemetry: Path
    log: Path


SURFACES = (
    _Surface(
        name="transactional",
        task_name="app.tasks.outbox_poller.run",
        relation="public.transactional_outbox",
        id_column="id",
        success_key="processed",
    ),
    _Surface(
        name="lifecycle",
        task_name="app.tasks.branch_outbox_poller.run",
        relation="public.branch_outbox_events",
        id_column="outbox_id",
        success_key="dead_lettered",
    ),
)


def _required_url(name: str):
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"P5-W2 runtime requires {name}")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-W2 {name} must include host, port and database")
    return url


def _safe_database_topology() -> tuple[str, int, str]:
    if os.environ.get("P5W2_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-W2 destructive process faults require explicit CI enablement")
    runtime = _required_url("TEST_DATABASE_URL")
    admin = _required_url("TEST_ADMIN_DATABASE_URL")
    runtime_topology = (str(runtime.host), int(runtime.port), str(runtime.database))
    admin_topology = (str(admin.host), int(admin.port), str(admin.database))
    if runtime_topology != admin_topology:
        raise RuntimeError("P5-W2 runtime/admin URLs must target one disposable topology")
    if runtime_topology[0] not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-W2 database host: {runtime_topology[0]}")
    if runtime_topology[2] != _DATABASE:
        raise RuntimeError(f"unsafe P5-W2 database: {runtime_topology[2]}")
    if os.environ.get("P5W2_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P5-W2 disposable database acknowledgement is absent")
    return runtime_topology


def _safe_broker() -> redis.Redis:
    raw = os.environ.get("CELERY_BROKER_URL", "")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "redis"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != 6379
        or parsed.path != "/1"
    ):
        raise RuntimeError(f"unsafe P5-W2 broker URL: {raw!r}")
    if os.environ.get("P5W2_DISPOSABLE_BROKER") != "redis-db-1":
        raise RuntimeError("P5-W2 disposable broker acknowledgement is absent")
    client = redis.Redis.from_url(raw, decode_responses=True)
    if client.ping() is not True:
        raise RuntimeError("P5-W2 Redis broker did not answer PING")
    return client


def _connect(login: str, password_environment: str):
    host, port, database = _safe_database_topology()
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=login,
        password=os.environ[password_environment],
    )


def _insert_canonical_initial_branch_state(
    *, org_id: uuid.UUID, owner_id: uuid.UUID, branch_id: uuid.UUID
) -> None:
    """Create state through the certified P3A auth bootstrap boundary."""
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
                (str(org_id), str(owner_id)),
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
                    %s,%s,'active',true,true,true,'active',true,NULL,NULL,
                    'api',NULL,NULL,false,NULL,NULL,NULL,0,1,NULL,NULL,NULL,
                    NULL,NULL,NULL,NULL,NULL,1,0,%s,NULL,NULL,NULL
                )
                RETURNING status_changed_at,updated_at
                """,
                (
                    branch_id,
                    org_id,
                    uuid.uuid4().hex[:26].upper(),
                ),
            )
            returned = cursor.fetchone()
            assert returned is not None
            assert all(value is not None for value in returned)
        connection.commit()


def _seed(surface: _Surface) -> _Seed:
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    event_id = uuid.uuid4()
    correlation_id = uuid.uuid4()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P5-W2 Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p5w2-runtime-{org_id.hex}"),
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.owners(
                    id,org_id,owner_name,email,hashed_password,
                    email_verified,onboarding_completed
                ) VALUES (
                    %s,%s,'P5-W2 Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (owner_id, org_id, f"p5w2-{owner_id.hex}@example.test"),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    timezone,currency_code,region_code,country_code
                ) VALUES (
                    %s,%s,'P5-W2 Runtime Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    branch_id,
                    org_id,
                    f"P5W2-{branch_id.hex[:8]}",
                    f"p5w2-{branch_id.hex}",
                ),
            )
        connection.commit()

    _insert_canonical_initial_branch_state(
        org_id=org_id,
        owner_id=owner_id,
        branch_id=branch_id,
    )

    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            if surface.name == "transactional":
                cursor.execute(
                    """
                    SELECT
                        pg_catalog.set_config('app.current_org_id',%s,true),
                        pg_catalog.set_config('app.current_role','owner',true),
                        pg_catalog.set_config('app.current_user_id',%s,true),
                        pg_catalog.set_config('app.current_principal_type','owner',true),
                        pg_catalog.set_config('app.current_gym_id','',true)
                    """,
                    (str(org_id), str(owner_id)),
                )
                cursor.execute(
                    "SELECT public.enqueue_branch_hours_rebuild(%s,%s)",
                    (branch_id, correlation_id),
                )
                event_id = cursor.fetchone()[0]
            else:
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
                # A malformed saga is deliberately fail-closed.  It exercises
                # lifecycle queue claim/reclaim and terminal durability without
                # crossing into provider, Finance or P5-C compensation scope.
                cursor.execute(
                    """
                    INSERT INTO public.branch_outbox_events(
                        outbox_id,tenant_id,branch_id,event_type,payload,
                        max_attempts,correlation_id
                    ) VALUES (%s,%s,%s,'branch.lifecycle_saga','{}'::jsonb,3,%s)
                    """,
                    (event_id, org_id, branch_id, correlation_id),
                )
        connection.commit()

    return _Seed(surface, org_id, owner_id, branch_id, event_id)


def _telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _wait_for(
    predicate: Callable[[], Any],
    *,
    description: str,
    timeout: float = _TASK_TIMEOUT_SECONDS,
) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last_value!r}")


def _worker_log(worker: _Worker) -> str:
    return worker.log.read_text(encoding="utf-8") if worker.log.exists() else ""


def _wait_until_ready(worker: _Worker) -> None:
    from app.core.celery_app import celery_app

    def ready() -> bool:
        if worker.process.poll() is not None:
            raise AssertionError(
                f"P5-W2 Celery worker exited during startup:\n{_worker_log(worker)}"
            )
        replies = celery_app.control.ping(
            destination=[worker.hostname],
            timeout=1.0,
        )
        return any(reply.get(worker.hostname, {}).get("ok") == "pong" for reply in replies)

    _wait_for(ready, description=f"Celery worker {worker.hostname} readiness")


@contextmanager
def _running_worker(
    tmp_path: Path,
    *,
    target_event_id: uuid.UUID | None = None,
    fault_mode: str = "",
    barrier_expected: int = 0,
) -> Iterator[_Worker]:
    token = uuid.uuid4().hex
    hostname = f"p5w2-{token}@localhost"
    queue = f"p5w2-{token}"
    telemetry = tmp_path / f"telemetry-{token}.jsonl"
    sentinel = tmp_path / f"fault-{token}.sentinel"
    log = tmp_path / f"worker-{token}.log"
    environment = os.environ.copy()
    # The pytest controller owns fixture/bootstrap credentials.  The process
    # under test runs the real production worker profile and must receive only
    # its worker database binding; the production bootstep attests that live
    # login before consuming from Redis.
    for forbidden_name in (
        "DATABASE_URL",
        "AUTH_DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "FINANCE_CONFIG_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_ADMIN_DATABASE_URL",
        "MIGRATION_PASSWORD",
        "APP_RUNTIME_PASSWORD",
        "WORKER_RUNTIME_PASSWORD",
    ):
        environment[forbidden_name] = ""
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
            "P5W2_TELEMETRY_PATH": str(telemetry),
            "P5W2_FAULT_SENTINEL": str(sentinel),
            "P5W2_TARGET_EVENT_ID": str(target_event_id or ""),
            "P5W2_FAULT_MODE": fault_mode,
            "P5W2_BARRIER_EXPECTED": str(barrier_expected),
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.core.celery_app:celery_app",
        "worker",
        "--pool=prefork",
        "--concurrency=2",
        "--prefetch-multiplier=1",
        f"--queues={queue}",
        f"--hostname={hostname}",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "--loglevel=INFO",
        "--include=scripts.ci.p5w2_celery_fault_hooks",
    ]
    with log.open("w", encoding="utf-8") as log_stream:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        worker = _Worker(process, hostname, queue, telemetry, log)
        try:
            _wait_until_ready(worker)
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
                    process.wait(timeout=10)


def _send(surface: _Surface, queue: str, *, task_id: str | None = None):
    from app.core.celery_app import celery_app

    return celery_app.send_task(surface.task_name, queue=queue, task_id=task_id)


def _result(async_result) -> dict[str, int]:
    value = async_result.get(
        timeout=_TASK_TIMEOUT_SECONDS,
        propagate=True,
        disable_sync_subtasks=False,
    )
    assert isinstance(value, dict)
    return {str(key): int(item) for key, item in value.items()}


def _task_processes(worker: _Worker, task_id: str) -> list[int]:
    return [
        int(record["pid"])
        for record in _telemetry(worker.telemetry)
        if record.get("event") == "task_prerun"
        and record.get("task_id") == task_id
    ]


def _fault_record(worker: _Worker, boundary: str) -> dict[str, Any] | None:
    return next(
        (
            record
            for record in _telemetry(worker.telemetry)
            if record.get("event") == boundary
        ),
        None,
    )


def _broker_drained(client: redis.Redis, queue: str) -> bool:
    return (
        client.llen(queue) == 0
        and client.hlen("unacked") == 0
        and client.zcard("unacked_index") == 0
    )


def _expire_claim(seed: _Seed) -> None:
    allowed = {
        ("public.transactional_outbox", "id"),
        ("public.branch_outbox_events", "outbox_id"),
    }
    if (seed.surface.relation, seed.surface.id_column) not in allowed:
        raise ValueError("P5-W2 attempted to expire an unapproved relation")
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {seed.surface.relation}
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE {seed.surface.id_column}=%s
                  AND leased_by IS NOT NULL
                """,
                (seed.event_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()


def _state(seed: _Seed) -> tuple[Any, ...]:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            if seed.surface.name == "transactional":
                cursor.execute(
                    """
                    SELECT processed_at IS NOT NULL,dead_lettered_at IS NOT NULL,
                           delivery_attempts,lease_fence,leased_by IS NOT NULL
                    FROM public.transactional_outbox WHERE id=%s
                    """,
                    (seed.event_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT status,attempt_count,lease_fence,leased_by IS NOT NULL
                    FROM public.branch_outbox_events WHERE outbox_id=%s
                    """,
                    (seed.event_id,),
                )
            row = cursor.fetchone()
            assert row is not None
            return tuple(row)


def _projection(seed: _Seed) -> tuple[int, int | None]:
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_org_id',%s,true),
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true)
                """,
                (str(seed.org_id), str(seed.owner_id)),
            )
            cursor.execute(
                """
                SELECT count(*),max(projection_version)
                FROM public.branch_hours_projection WHERE branch_id=%s
                """,
                (seed.branch_id,),
            )
            count, version = cursor.fetchone()
            return int(count), None if version is None else int(version)


def _assert_claimed_not_committed(seed: _Seed) -> None:
    if seed.surface.name == "transactional":
        assert _state(seed) == (False, False, 1, 1, True)
        assert _projection(seed) == (0, None)
    else:
        assert _state(seed) == ("processing", 1, 1, True)


def _assert_single_terminal_effect(seed: _Seed, *, fence: int) -> None:
    if seed.surface.name == "transactional":
        assert _state(seed) == (True, False, 1, fence, False)
        assert _projection(seed) == (1, 1)
        with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) FROM public.transactional_outbox
                    WHERE id=%s OR parent_event_id=%s
                    """,
                    (seed.event_id, seed.event_id),
                )
                assert cursor.fetchone()[0] == 1
    else:
        assert _state(seed) == ("dead_lettered", 1, fence, False)


@pytest.fixture(scope="session", autouse=True)
def _disposable_runtime() -> Iterator[redis.Redis]:
    _safe_database_topology()
    client = _safe_broker()
    client.flushdb()
    yield client
    client.flushdb()


def test_projection_policy_is_app_scoped_without_worker_pii_access() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT policy_data.polcmd::text,
                       policy_data.polpermissive,
                       policy_data.polroles,
                       app_role.oid
                FROM pg_catalog.pg_policy AS policy_data
                CROSS JOIN pg_catalog.pg_roles AS app_role
                WHERE policy_data.polrelid=
                          'public.branch_hours_projection'::regclass
                  AND policy_data.polname='tenant_isolation_projection'
                  AND app_role.rolname='app_runtime'
                """
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "*"
            assert row[1] is True
            assert list(row[2]) == [row[3]]
            cursor.execute(
                """
                SELECT pg_catalog.has_table_privilege(
                    'worker_runtime','public.organization_members','SELECT'
                )
                """
            )
            assert cursor.fetchone() == (False,)
        connection.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            with pytest.raises(InsufficientPrivilege):
                cursor.execute(
                    "SELECT id FROM public.organization_members LIMIT 1"
                )
        connection.rollback()


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_real_worker_death_before_commit_is_reclaimed_by_replacement(
    surface: _Surface,
    tmp_path: Path,
) -> None:
    client = _safe_broker()
    seed = _seed(surface)
    boundary = "before_db_commit_kill"
    with _running_worker(
        tmp_path,
        target_event_id=seed.event_id,
        fault_mode="before_db_commit",
    ) as crashed_worker:
        crashed = _send(surface, crashed_worker.queue)
        fault = _wait_for(
            lambda: _fault_record(crashed_worker, boundary),
            description=f"{surface.name} pre-commit child death",
        )
        processes = _wait_for(
            lambda: (
                values
                if len(values := _task_processes(crashed_worker, crashed.id)) >= 2
                else None
            ),
            description=f"{surface.name} broker redelivery after pre-commit death",
        )
        _wait_for(
            lambda: _broker_drained(client, crashed_worker.queue),
            description=f"{surface.name} redelivered task acknowledgement",
        )
        assert int(fault["pid"]) in processes
        assert any(pid != int(fault["pid"]) for pid in processes)
        _assert_claimed_not_committed(seed)
        crashed_hostname = crashed_worker.hostname
        killed_pid = int(fault["pid"])

    # Stop the first Celery parent as well, then prove that a separately
    # started worker instance can reclaim the expired durable command.
    _expire_claim(seed)
    with _running_worker(tmp_path) as replacement_worker:
        assert replacement_worker.hostname != crashed_hostname
        recovered = _send(surface, replacement_worker.queue)
        result = _result(recovered)
        assert result["claimed"] == 1
        assert result[surface.success_key] == 1
        recovery_processes = _task_processes(replacement_worker, recovered.id)
        assert recovery_processes
        assert all(pid != killed_pid for pid in recovery_processes)
        _wait_for(
            lambda: _broker_drained(client, replacement_worker.queue),
            description=f"{surface.name} replacement recovery acknowledgement",
        )
        _assert_single_terminal_effect(seed, fence=2)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_real_worker_death_after_commit_redelivers_without_repeating_effect(
    surface: _Surface,
    tmp_path: Path,
) -> None:
    client = _safe_broker()
    seed = _seed(surface)
    boundary = "after_db_commit_before_task_ack_kill"
    with _running_worker(
        tmp_path,
        target_event_id=seed.event_id,
        fault_mode="after_db_commit_before_task_ack",
    ) as worker:
        crashed = _send(surface, worker.queue)
        fault = _wait_for(
            lambda: _fault_record(worker, boundary),
            description=f"{surface.name} post-commit pre-ack child death",
        )
        processes = _wait_for(
            lambda: (
                values
                if len(values := _task_processes(worker, crashed.id)) >= 2
                else None
            ),
            description=f"{surface.name} same-task broker redelivery",
        )
        _wait_for(
            lambda: _broker_drained(client, worker.queue),
            description=f"{surface.name} post-commit redelivery acknowledgement",
        )
        assert int(fault["pid"]) in processes
        assert any(pid != int(fault["pid"]) for pid in processes)
        assert fault["outcome"] == surface.success_key
        _assert_single_terminal_effect(seed, fence=1)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_sequential_duplicate_celery_delivery_converges_once(
    surface: _Surface,
    tmp_path: Path,
) -> None:
    client = _safe_broker()
    seed = _seed(surface)
    with _running_worker(tmp_path) as worker:
        duplicate_task_id = str(uuid.uuid4())
        first = _result(_send(surface, worker.queue, task_id=duplicate_task_id))
        _send(surface, worker.queue, task_id=duplicate_task_id)
        processes = _wait_for(
            lambda: (
                values
                if len(values := _task_processes(worker, duplicate_task_id)) >= 2
                else None
            ),
            description=f"{surface.name} sequential same-task duplicate",
        )
        assert first["claimed"] == 1
        assert first[surface.success_key] == 1
        assert len(processes) == 2
        _wait_for(
            lambda: _broker_drained(client, worker.queue),
            description=f"{surface.name} sequential duplicate acknowledgement",
        )
        _assert_single_terminal_effect(seed, fence=1)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda surface: surface.name)
def test_concurrent_duplicate_celery_delivery_converges_once(
    surface: _Surface,
    tmp_path: Path,
) -> None:
    client = _safe_broker()
    seed = _seed(surface)
    with _running_worker(tmp_path, barrier_expected=2) as worker:
        duplicate_task_id = str(uuid.uuid4())
        _send(surface, worker.queue, task_id=duplicate_task_id)
        _send(surface, worker.queue, task_id=duplicate_task_id)
        processes = _wait_for(
            lambda: (
                values
                if len(values := _task_processes(worker, duplicate_task_id)) >= 2
                else None
            ),
            description=f"{surface.name} concurrent same-task duplicate",
        )
        assert len(set(processes)) == 2
        _wait_for(
            lambda: _broker_drained(client, worker.queue),
            description=f"{surface.name} concurrent duplicate acknowledgement",
        )
        _assert_single_terminal_effect(seed, fence=1)
