from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]
_DATABASE = "gymflow_p5c_test"
_ADMIN_LOGIN = "migration_owner"
_AUTH_LOGIN = "auth_p5c_runtime"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_TIMEOUT = 15.0


@dataclass(frozen=True)
class _Seed:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    branch_ids: tuple[uuid.UUID, ...]


def _required_url(name: str):
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"P5-C runtime requires {name}")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-C {name} must include host, port and database")
    return url


def _safe_database_topology() -> tuple[str, int, str]:
    if os.environ.get("P5C_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-C destructive process faults require explicit CI enablement")
    runtime = _required_url("TEST_DATABASE_URL")
    admin = _required_url("TEST_ADMIN_DATABASE_URL")
    runtime_topology = (str(runtime.host), int(runtime.port), str(runtime.database))
    admin_topology = (str(admin.host), int(admin.port), str(admin.database))
    if runtime_topology != admin_topology:
        raise RuntimeError("P5-C runtime/admin URLs must target one disposable topology")
    if runtime_topology[0] not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-C database host: {runtime_topology[0]}")
    if runtime_topology[2] != _DATABASE:
        raise RuntimeError(f"unsafe P5-C database: {runtime_topology[2]}")
    if os.environ.get("P5C_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P5-C disposable database acknowledgement is absent")
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


def _infra_psql(sql: str, *, tuples_only: bool = False) -> str:
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
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-C CI infrastructure SQL failed:\n{completed.stdout}")
    return completed.stdout.strip()


def _wait_for(predicate, *, description: str, timeout: float = _TIMEOUT):
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def _insert_initial_state(seed: _Seed, branch_id: uuid.UUID, *, is_primary: bool) -> None:
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


def _seed() -> _Seed:
    seed = _Seed(
        org_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        branch_ids=(uuid.uuid4(), uuid.uuid4()),
    )
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P5-C Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (seed.org_id, f"p5c-runtime-{seed.org_id.hex}"),
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
                    %s,%s,'P5-C Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (seed.owner_id, seed.org_id, f"p5c-{seed.owner_id.hex}@example.test"),
            )
            cursor.execute(
                """
                INSERT INTO public.organization_users(
                    id,org_id,name,email,password_hash,is_active,is_verified
                ) VALUES (
                    %s,%s,'P5-C Runtime Owner User',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (seed.owner_id, seed.org_id, f"p5c-{seed.owner_id.hex}@example.test"),
            )
            for index, branch_id in enumerate(seed.branch_ids, start=1):
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
                        f"P5-C Branch {index}",
                        f"P5C-{branch_id.hex[:8]}",
                        f"p5c-{branch_id.hex}",
                    ),
                )
        connection.commit()

    for index, branch_id in enumerate(seed.branch_ids):
        _insert_initial_state(seed, branch_id, is_primary=index == 0)
    return seed


def _async_app_url():
    return _required_url("TEST_DATABASE_URL").set(drivername="postgresql+asyncpg")


async def _transition(seed: _Seed, branch_id: uuid.UUID) -> uuid.UUID:
    from app.core.database import update_session_context
    from app.services.branch_lifecycle_service import BranchLifecycleService

    engine = create_async_engine(_async_app_url(), poolclass=NullPool)
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
            return await BranchLifecycleService(session).initiate_transition(
                branch_id=branch_id,
                org_id=seed.org_id,
                to_status="temporarily_closed",
                actor_id=seed.owner_id,
                actor_role="owner",
                transition_source="api",
            )
    finally:
        await engine.dispose()


def _find_parent(correlation_id: uuid.UUID) -> uuid.UUID:
    output = _infra_psql(
        "SELECT outbox_id::text "
        "FROM public.branch_outbox_events "
        f"WHERE correlation_id='{correlation_id}'::uuid "
        "AND event_type='branch.lifecycle_saga' "
        "ORDER BY outbox_id;",
        tuples_only=True,
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(rows) == 1
    return uuid.UUID(rows[0])


def _postpone_other_pending(parent_id: uuid.UUID) -> None:
    _infra_psql(
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp()+INTERVAL '1 hour' "
        "WHERE status='pending' "
        f"AND outbox_id<>'{parent_id}'::uuid;"
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp() "
        f"WHERE status='pending' AND outbox_id='{parent_id}'::uuid;"
    )


async def _claim_specific(parent_id: uuid.UUID, worker_id: uuid.UUID) -> dict[str, Any]:
    from app.tasks.branch_outbox_poller import _claim_events

    _postpone_other_pending(parent_id)
    events = await _claim_events(worker_id)
    matches = [event for event in events if event["outbox_id"] == parent_id]
    assert len(matches) == 1
    return matches[0]


async def _exhaust_owned_event(
    event: dict[str, Any],
    worker_id: uuid.UUID,
) -> dict[str, Any]:
    from app.core.database import worker_async_session_maker
    from app.tasks.branch_outbox_poller import _install_saga_context

    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        result = await session.execute(
            text(
                """
                UPDATE public.branch_outbox_events
                SET attempt_count=max_attempts
                WHERE outbox_id=:outbox_id
                  AND status='processing'
                  AND leased_by=:worker_id
                  AND lease_fence=:lease_fence
                  AND leased_until > pg_catalog.clock_timestamp()
                RETURNING attempt_count,max_attempts
                """
            ),
            {
                "outbox_id": event["outbox_id"],
                "worker_id": worker_id,
                "lease_fence": int(event["lease_fence"]),
            },
        )
        row = result.one()
        await session.commit()

    exhausted = dict(event)
    exhausted["attempt_count"] = int(row.attempt_count)
    exhausted["max_attempts"] = int(row.max_attempts)
    assert exhausted["attempt_count"] == exhausted["max_attempts"]
    return exhausted


def _drop_precommit_barrier() -> None:
    _infra_psql(
        """
        DROP TRIGGER IF EXISTS p5c_compensation_precommit_barrier
          ON public.branch_lifecycle_events;
        DROP FUNCTION IF EXISTS public.p5c_compensation_precommit_barrier();
        """
    )


def _install_precommit_barrier(branch_id: uuid.UUID) -> None:
    _drop_precommit_barrier()
    _infra_psql(
        f"""
        CREATE FUNCTION public.p5c_compensation_precommit_barrier()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.event_type='compensation_completed'
             AND NEW.branch_id='{branch_id}'::uuid THEN
            PERFORM pg_catalog.pg_sleep(30);
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER p5c_compensation_precommit_barrier
        BEFORE INSERT ON public.branch_lifecycle_events
        FOR EACH ROW
        EXECUTE FUNCTION public.p5c_compensation_precommit_barrier();
        """
    )


def _wait_for_sleeping_compensation(
    process: subprocess.Popen[str],
    log_file: Path,
) -> int:
    def probe() -> int | None:
        if process.poll() is not None:
            raise AssertionError(
                "P5-C compensation child exited before pre-commit barrier "
                f"rc={process.returncode}:\n{_process_log(log_file)}"
            )
        output = _infra_psql(
            "SELECT pid FROM pg_catalog.pg_stat_activity "
            "WHERE datname='gymflow_p5c_test' "
            "AND usename='worker_test_runtime' "
            "AND state='active' "
            "AND wait_event='PgSleep' "
            "AND query ILIKE '%branch_lifecycle_events%' "
            "ORDER BY pid LIMIT 1;",
            tuples_only=True,
        )
        value = output.strip().splitlines()[-1] if output.strip() else ""
        return int(value) if value.isdigit() else None

    return int(
        _wait_for(
            probe,
            description="P5-C worker sleeping inside pre-commit compensation barrier",
            timeout=8.0,
        )
    )


def _json_event(event: dict[str, Any]) -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, uuid.UUID):
            encoded[key] = str(value)
        else:
            encoded[key] = value
    return encoded


def _restore_event(raw: dict[str, Any]) -> dict[str, Any]:
    restored = dict(raw)
    for key in ("outbox_id", "tenant_id", "branch_id", "correlation_id"):
        restored[key] = uuid.UUID(str(restored[key]))
    restored["attempt_count"] = int(restored["attempt_count"])
    restored["max_attempts"] = int(restored["max_attempts"])
    restored["lease_fence"] = int(restored["lease_fence"])
    return restored


def _spawn_compensation_process(
    tmp_path: Path,
    *,
    event: dict[str, Any],
    worker_id: uuid.UUID,
    after_commit_pause: bool,
) -> tuple[subprocess.Popen[str], Path, Path, Path]:
    token = uuid.uuid4().hex
    event_file = tmp_path / f"event-{token}.json"
    commit_marker = tmp_path / f"commit-{token}.txt"
    ack_marker = tmp_path / f"ack-{token}.txt"
    log_file = tmp_path / f"worker-{token}.log"
    event_file.write_text(json.dumps(_json_event(event), sort_keys=True), encoding="utf-8")

    environment = os.environ.copy()
    worker_database_url = environment.get("WORKER_DATABASE_URL", "").strip()
    if not worker_database_url:
        raise RuntimeError("P5-C killable child requires WORKER_DATABASE_URL")
    for forbidden_name in (
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
        environment[forbidden_name] = ""
    # Non-production Settings validates DATABASE_URL and app.core.database builds
    # all engines at import time. Give that bootstrap the exact worker URL so the
    # crash process has no database authority beyond worker_test_runtime.
    environment["DATABASE_URL"] = worker_database_url
    environment["PYTHONUNBUFFERED"] = "1"

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--p5c-worker",
        str(event_file),
        str(worker_id),
        str(commit_marker),
        str(ack_marker),
        "1" if after_commit_pause else "0",
    ]
    log_handle = log_file.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log_handle.close()
    return process, commit_marker, ack_marker, log_file


def _kill_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    assert process.returncode is not None and process.returncode != 0


def _process_log(log_file: Path) -> str:
    return log_file.read_text(encoding="utf-8") if log_file.exists() else ""


def _expire_killed_lease(
    parent_id: uuid.UUID,
    *,
    worker_id: uuid.UUID,
    lease_fence: int,
) -> None:
    output = _infra_psql(
        "WITH changed AS ("
        "UPDATE public.branch_outbox_events "
        "SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
        f"WHERE outbox_id='{parent_id}'::uuid "
        "AND status='processing' "
        f"AND leased_by='{worker_id}'::uuid "
        f"AND lease_fence={lease_fence} "
        "RETURNING 1"
        ") SELECT count(*) FROM changed;",
        tuples_only=True,
    )
    assert output.strip().splitlines()[-1] == "1"


def _branch_state(branch_id: uuid.UUID) -> tuple[str, bool, bool, str | None, str | None]:
    output = _infra_psql(
        "SELECT status || '|' || is_operational::text || '|' || "
        "lifecycle_transition_in_progress::text || '|' || "
        "COALESCE(transition_source,'NULL') || '|' || "
        "COALESCE(saga_compensation_strategy,'NULL') "
        "FROM public.org_branch_state "
        f"WHERE branch_id='{branch_id}'::uuid;",
        tuples_only=True,
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(rows) == 1
    values = rows[0].split("|")
    assert len(values) == 5
    return (
        values[0],
        values[1] == "true",
        values[2] == "true",
        None if values[3] == "NULL" else values[3],
        None if values[4] == "NULL" else values[4],
    )


def _parent_state(parent_id: uuid.UUID) -> tuple[str, int, int, int, str | None, bool]:
    output = _infra_psql(
        "SELECT status || '|' || attempt_count::text || '|' || "
        "max_attempts::text || '|' || lease_fence::text || '|' || "
        "COALESCE(leased_by::text,'NULL') || '|' || "
        "(leased_until IS NOT NULL)::text "
        "FROM public.branch_outbox_events "
        f"WHERE outbox_id='{parent_id}'::uuid;",
        tuples_only=True,
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(rows) == 1
    values = rows[0].split("|")
    assert len(values) == 6
    return (
        values[0],
        int(values[1]),
        int(values[2]),
        int(values[3]),
        None if values[4] == "NULL" else values[4],
        values[5] == "true",
    )


def _compensation_counts(correlation_id: uuid.UUID) -> tuple[int, int, int]:
    output = _infra_psql(
        "SELECT "
        "(SELECT count(*) FROM public.branch_lifecycle_events "
        f" WHERE correlation_id='{correlation_id}'::uuid "
        " AND event_type='compensation_completed')::text || '|' || "
        "(SELECT count(*) FROM public.branch_status_history "
        f" WHERE correlation_id='{correlation_id}'::uuid "
        " AND transition_source='saga_compensation')::text || '|' || "
        "(SELECT count(*) FROM public.branch_outbox_events "
        f" WHERE correlation_id='{correlation_id}'::uuid "
        " AND event_type='branch.search_index' "
        " AND payload->>'reason'='saga_dead_letter_compensation')::text;",
        tuples_only=True,
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(rows) == 1
    values = rows[0].split("|")
    assert len(values) == 3
    return int(values[0]), int(values[1]), int(values[2])


def _prepare_exhausted_parent(seed: _Seed) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any], uuid.UUID]:
    branch_id = seed.branch_ids[0]
    correlation_id = asyncio.run(_transition(seed, branch_id))
    parent_id = _find_parent(correlation_id)
    worker_id = uuid.uuid4()
    claimed = asyncio.run(_claim_specific(parent_id, worker_id))
    exhausted = asyncio.run(_exhaust_owned_event(claimed, worker_id))
    return branch_id, correlation_id, exhausted, worker_id


def test_precommit_process_death_rolls_back_and_replacement_worker_recovers(tmp_path: Path) -> None:
    _safe_database_topology()
    seed = _seed()
    branch_id, correlation_id, exhausted, killed_worker = _prepare_exhausted_parent(seed)
    parent_id = exhausted["outbox_id"]
    original_fence = int(exhausted["lease_fence"])
    _install_precommit_barrier(branch_id)

    process, commit_marker, ack_marker, log_file = _spawn_compensation_process(
        tmp_path,
        event=exhausted,
        worker_id=killed_worker,
        after_commit_pause=False,
    )
    try:
        _wait_for_sleeping_compensation(process, log_file)
        assert not commit_marker.exists()
        assert not ack_marker.exists()
        _kill_process(process)
    finally:
        if process.poll() is None:
            _kill_process(process)
        _drop_precommit_barrier()

    assert _branch_state(branch_id) == (
        "temporarily_closed",
        False,
        True,
        "api",
        "rollback_to_origin",
    )
    assert _compensation_counts(correlation_id) == (0, 0, 0)
    status, attempts, max_attempts, fence, leased_by, has_lease = _parent_state(parent_id)
    assert status == "processing"
    assert attempts == max_attempts
    assert fence == original_fence
    assert leased_by == str(killed_worker)
    assert has_lease is True

    _expire_killed_lease(
        parent_id,
        worker_id=killed_worker,
        lease_fence=original_fence,
    )
    replacement_worker = uuid.uuid4()
    replacement_event = asyncio.run(_claim_specific(parent_id, replacement_worker))
    assert int(replacement_event["lease_fence"]) == original_fence + 1
    assert int(replacement_event["attempt_count"]) == int(replacement_event["max_attempts"])

    from app.tasks.branch_outbox_poller import _fail_event

    outcome = asyncio.run(
        _fail_event(
            replacement_event,
            replacement_worker,
            RuntimeError("P5-C replacement compensation after pre-commit worker death"),
            permanent=False,
        )
    )
    assert outcome == "dead_lettered_compensated", _process_log(log_file)
    assert _branch_state(branch_id) == (
        "active",
        True,
        False,
        "saga_compensation",
        None,
    )
    assert _compensation_counts(correlation_id) == (1, 1, 1)
    terminal = _parent_state(parent_id)
    assert terminal[0] == "dead_lettered"
    assert terminal[4] is None
    assert terminal[5] is False


def test_after_commit_process_death_redelivery_is_single_effect(tmp_path: Path) -> None:
    _safe_database_topology()
    seed = _seed()
    branch_id, correlation_id, exhausted, committed_worker = _prepare_exhausted_parent(seed)
    parent_id = exhausted["outbox_id"]

    process, commit_marker, ack_marker, log_file = _spawn_compensation_process(
        tmp_path,
        event=exhausted,
        worker_id=committed_worker,
        after_commit_pause=True,
    )
    try:
        def committed() -> bool:
            if process.poll() is not None:
                raise AssertionError(
                    "P5-C compensation child exited before post-commit hold "
                    f"rc={process.returncode}:\n{_process_log(log_file)}"
                )
            return commit_marker.exists() and (
                commit_marker.read_text(encoding="utf-8").strip()
                == "dead_lettered_compensated"
            )

        _wait_for(
            committed,
            description="P5-C compensation commit marker before task acknowledgement",
            timeout=12.0,
        )
        assert process.poll() is None, _process_log(log_file)
        assert not ack_marker.exists()
        assert _branch_state(branch_id) == (
            "active",
            True,
            False,
            "saga_compensation",
            None,
        )
        assert _parent_state(parent_id)[0] == "dead_lettered"
        assert _compensation_counts(correlation_id) == (1, 1, 1)
        _kill_process(process)
    finally:
        if process.poll() is None:
            _kill_process(process)

    assert not ack_marker.exists()

    from app.tasks.branch_outbox_poller import _fail_event

    replay_worker = uuid.uuid4()
    replay_outcome = asyncio.run(
        _fail_event(
            exhausted,
            replay_worker,
            RuntimeError("P5-C redelivery after committed compensation worker death"),
            permanent=False,
        )
    )
    assert replay_outcome == "lease_lost"
    assert _branch_state(branch_id) == (
        "active",
        True,
        False,
        "saga_compensation",
        None,
    )
    assert _parent_state(parent_id)[0] == "dead_lettered"
    assert _compensation_counts(correlation_id) == (1, 1, 1)


async def _child_fail_event(
    event_file: Path,
    worker_id: uuid.UUID,
) -> str:
    from app.tasks.branch_outbox_poller import _fail_event

    event = _restore_event(json.loads(event_file.read_text(encoding="utf-8")))
    return await _fail_event(
        event,
        worker_id,
        RuntimeError("P5-C injected exhausted Transaction-B failure"),
        permanent=False,
    )


def _child_main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[0] != "--p5c-worker":
        return 2
    event_file = Path(argv[1])
    worker_id = uuid.UUID(argv[2])
    commit_marker = Path(argv[3])
    ack_marker = Path(argv[4])
    after_commit_pause = argv[5] == "1"

    outcome = asyncio.run(_child_fail_event(event_file, worker_id))
    commit_marker.write_text(outcome, encoding="utf-8")
    if after_commit_pause:
        time.sleep(60)
    ack_marker.write_text("acked", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
