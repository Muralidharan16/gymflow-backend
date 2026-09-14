from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.errors import DeadlockDetected
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.tasks.branch_outbox_poller import (
    _claim_events,
    _install_saga_context,
    _mark_delivered,
    _process_refund_required_event,
    _process_saga_event,
)


_DATABASE = "gymflow_p5r_test"
_ADMIN_LOGIN = "migration_owner"
_AUTH_LOGIN = "auth_p5r_runtime"
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
        raise RuntimeError(f"P5-R runtime requires {name}")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-R {name} must include host, port and database")
    return url


def _safe_database_topology() -> tuple[str, int, str]:
    if os.environ.get("P5R_RACE_FAULTS") != "1":
        raise RuntimeError("P5-R destructive concurrency proofs require explicit CI enablement")
    runtime = _required_url("TEST_DATABASE_URL")
    admin = _required_url("TEST_ADMIN_DATABASE_URL")
    runtime_topology = (str(runtime.host), int(runtime.port), str(runtime.database))
    admin_topology = (str(admin.host), int(admin.port), str(admin.database))
    if runtime_topology != admin_topology:
        raise RuntimeError("P5-R runtime/admin URLs must target one disposable topology")
    host, _port, database = runtime_topology
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-R database host: {host}")
    if database != _DATABASE:
        raise RuntimeError(f"unsafe P5-R database: {database}")
    if os.environ.get("P5R_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P5-R disposable database acknowledgement is absent")
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
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-R CI infrastructure SQL failed:\n{completed.stdout}")
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


def _seed(branch_count: int = 2) -> _Seed:
    assert branch_count >= 1
    seed = _Seed(
        org_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        branch_ids=tuple(uuid.uuid4() for _ in range(branch_count)),
    )
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P5-R Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (seed.org_id, f"p5r-runtime-{seed.org_id.hex}"),
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
                    %s,%s,'P5-R Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (seed.owner_id, seed.org_id, f"p5r-{seed.owner_id.hex}@example.test"),
            )
            cursor.execute(
                """
                INSERT INTO public.organization_users(
                    id,org_id,name,email,password_hash,is_active,is_verified
                ) VALUES (
                    %s,%s,'P5-R Runtime Owner User',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (seed.owner_id, seed.org_id, f"p5r-{seed.owner_id.hex}@example.test"),
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
                        f"P5-R Branch {index}",
                        f"P5R-{branch_id.hex[:8]}",
                        f"p5r-{branch_id.hex}",
                    ),
                )
        connection.commit()

    for index, branch_id in enumerate(seed.branch_ids):
        _insert_initial_state(seed, branch_id, is_primary=index == 0)
    return seed


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


def _async_app_url():
    return _required_url("TEST_DATABASE_URL").set(drivername="postgresql+asyncpg")


async def _transition(seed: _Seed, branch_id: uuid.UUID, to_status: str) -> uuid.UUID:
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
            service = BranchLifecycleService(session)
            return await service.initiate_transition(
                branch_id=branch_id,
                org_id=seed.org_id,
                to_status=to_status,
                actor_id=seed.owner_id,
                actor_role="owner",
                transition_source="api",
            )
    finally:
        await engine.dispose()


async def _attempt_transition(seed: _Seed, branch_id: uuid.UUID, to_status: str):
    try:
        return ("ok", await _transition(seed, branch_id, to_status))
    except HTTPException as exc:
        return ("http", exc.status_code, str(exc.detail))
    except Exception as exc:  # evidence: DB failures must remain visible to assertions
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is None:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        return ("db", sqlstate, repr(exc))


def _branch_state(branch_id: uuid.UUID) -> tuple[str, bool, bool]:
    output = _admin_psql(
        "SELECT status,is_operational::text,lifecycle_transition_in_progress::text "
        "FROM public.org_branch_state "
        f"WHERE branch_id='{branch_id}'::uuid;",
        tuples_only=True,
    )
    values = output.splitlines()[-1].strip().split("|")
    assert len(values) == 3
    return values[0], values[1] == "true", values[2] == "true"


def _org_operational_count(org_id: uuid.UUID) -> int:
    output = _admin_psql(
        "SELECT count(*) FROM public.org_branch_state "
        f"WHERE org_id='{org_id}'::uuid AND is_operational IS TRUE "
        "AND deleted_at IS NULL;",
        tuples_only=True,
    )
    return int(output.splitlines()[-1].strip())


def _history_count(branch_id: uuid.UUID, from_status: str, to_status: str) -> int:
    output = _admin_psql(
        "SELECT count(*) FROM public.branch_status_history "
        f"WHERE branch_id='{branch_id}'::uuid "
        f"AND from_status='{from_status}' AND to_status='{to_status}';",
        tuples_only=True,
    )
    return int(output.splitlines()[-1].strip())


def _outbox_count(*, correlation_id: uuid.UUID | None = None, event_type: str | None = None) -> int:
    clauses = ["TRUE"]
    if correlation_id is not None:
        clauses.append(f"correlation_id='{correlation_id}'::uuid")
    if event_type is not None:
        escaped = event_type.replace("'", "''")
        clauses.append(f"event_type='{escaped}'")
    output = _admin_psql(
        "SELECT count(*) FROM public.branch_outbox_events WHERE " + " AND ".join(clauses) + ";",
        tuples_only=True,
    )
    return int(output.splitlines()[-1].strip())


def _find_correlated(correlation_id: uuid.UUID, event_type: str) -> uuid.UUID:
    escaped = event_type.replace("'", "''")
    output = _admin_psql(
        "SELECT outbox_id FROM public.branch_outbox_events "
        f"WHERE correlation_id='{correlation_id}'::uuid AND event_type='{escaped}' "
        "ORDER BY outbox_id;",
        tuples_only=True,
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(rows) == 1
    return uuid.UUID(rows[0])


def _outbox_state(outbox_id: uuid.UUID) -> tuple[str, int, int, str | None]:
    output = _admin_psql(
        "SELECT status,lease_fence::text,attempt_count::text,"
        "COALESCE(leased_by::text,'NULL') FROM public.branch_outbox_events "
        f"WHERE outbox_id='{outbox_id}'::uuid;",
        tuples_only=True,
    )
    values = output.splitlines()[-1].strip().split("|")
    assert len(values) == 4
    return values[0], int(values[1]), int(values[2]), None if values[3] == "NULL" else values[3]


def _insert_ready_outbox(seed: _Seed) -> uuid.UUID:
    outbox_id = uuid.uuid4()
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
                    %s,%s,%s,'branch.lifecycle_saga','{}'::jsonb,5,%s
                )
                """,
                (outbox_id, seed.org_id, seed.branch_ids[0], uuid.uuid4()),
            )
        connection.commit()
    return outbox_id


def _postpone_all_except(outbox_id: uuid.UUID) -> None:
    _admin_psql(
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp()+INTERVAL '1 hour' "
        "WHERE status='pending' "
        f"AND outbox_id<>'{outbox_id}'::uuid;"
        "UPDATE public.branch_outbox_events "
        "SET process_after=pg_catalog.clock_timestamp() "
        f"WHERE status='pending' AND outbox_id='{outbox_id}'::uuid;"
    )


def _expire_lease(outbox_id: uuid.UUID) -> None:
    _admin_psql(
        "UPDATE public.branch_outbox_events "
        "SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
        f"WHERE outbox_id='{outbox_id}'::uuid AND status='processing';"
    )


async def _claim_specific(outbox_id: uuid.UUID, worker_id: uuid.UUID) -> dict[str, Any]:
    _postpone_all_except(outbox_id)
    events = await _claim_events(worker_id)
    matches = [event for event in events if event["outbox_id"] == outbox_id]
    assert len(matches) == 1
    return matches[0]


async def _deliver_with_fence(event: dict[str, Any], worker_id: uuid.UUID) -> None:
    from app.core.database import worker_async_session_maker

    async with worker_async_session_maker() as session:
        await _install_saga_context(session, event=event, worker_id=worker_id)
        await _mark_delivered(
            session,
            outbox_id=event["outbox_id"],
            worker_id=worker_id,
            lease_fence=int(event["lease_fence"]),
        )
        await session.commit()


def _drop_barriers() -> None:
    _admin_psql(
        """
        DROP TRIGGER IF EXISTS p5r_transaction_b_barrier ON public.branch_lifecycle_events;
        DROP TRIGGER IF EXISTS p5r_refund_visibility_barrier ON public.branch_lifecycle_events;
        DROP FUNCTION IF EXISTS public.p5r_transaction_b_barrier();
        DROP FUNCTION IF EXISTS public.p5r_refund_visibility_barrier();
        """
    )


def _install_transaction_b_barrier(branch_id: uuid.UUID) -> None:
    _drop_barriers()
    _admin_psql(
        f"""
        CREATE FUNCTION public.p5r_transaction_b_barrier() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.event_type='transaction_b_started'
             AND NEW.branch_id='{branch_id}'::uuid THEN
            PERFORM pg_catalog.pg_sleep(2.5);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER p5r_transaction_b_barrier
          BEFORE INSERT ON public.branch_lifecycle_events
          FOR EACH ROW EXECUTE FUNCTION public.p5r_transaction_b_barrier();
        """
    )


def _install_refund_visibility_barrier(branch_id: uuid.UUID) -> None:
    _drop_barriers()
    _admin_psql(
        f"""
        CREATE FUNCTION public.p5r_refund_visibility_barrier() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.event_type='refunds_queued'
             AND NEW.branch_id='{branch_id}'::uuid THEN
            PERFORM pg_catalog.pg_sleep(2.5);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER p5r_refund_visibility_barrier
          BEFORE INSERT ON public.branch_lifecycle_events
          FOR EACH ROW EXECUTE FUNCTION public.p5r_refund_visibility_barrier();
        """
    )


def _wait_for_sleeping_worker() -> int:
    def probe() -> int | None:
        output = _admin_psql(
            "SELECT pid FROM pg_catalog.pg_stat_activity "
            "WHERE usename='worker_test_runtime' AND state='active' "
            "AND wait_event='PgSleep' AND query ILIKE '%branch_lifecycle_events%' "
            "ORDER BY pid LIMIT 1;",
            tuples_only=True,
        )
        value = output.strip().splitlines()[-1] if output.strip() else ""
        return int(value) if value.isdigit() else None

    return int(_wait_for(probe, description="P5-R sleeping Transaction-B worker", timeout=5.0))


def _finance_counts() -> tuple[int, int]:
    output = _admin_psql(
        "SELECT (SELECT count(*) FROM finance.refunds)::text || '|' || "
        "(SELECT count(*) FROM finance.refund_execution_commands)::text;",
        tuples_only=True,
    )
    values = output.splitlines()[-1].strip().split("|")
    assert len(values) == 2
    return int(values[0]), int(values[1])


def test_same_branch_transition_race_commits_exactly_one_transaction_a() -> None:
    _safe_database_topology()
    seed = _seed(2)
    branch_id = seed.branch_ids[0]

    async def race():
        return await asyncio.gather(
            _attempt_transition(seed, branch_id, "temporarily_closed"),
            _attempt_transition(seed, branch_id, "temporarily_closed"),
        )

    results = asyncio.run(race())
    assert sum(result[0] == "ok" for result in results) == 1
    losers = [result for result in results if result[0] != "ok"]
    assert len(losers) == 1
    assert losers[0][0:2] == ("http", 409)
    assert all(not (result[0] == "db" and result[1] == "40P01") for result in results)
    assert _branch_state(branch_id) == ("temporarily_closed", False, True)
    assert _history_count(branch_id, "active", "temporarily_closed") == 1


def test_last_operational_branch_race_preserves_org_invariant() -> None:
    _safe_database_topology()
    seed = _seed(2)

    async def race():
        return await asyncio.gather(
            _attempt_transition(seed, seed.branch_ids[0], "temporarily_closed"),
            _attempt_transition(seed, seed.branch_ids[1], "temporarily_closed"),
        )

    results = asyncio.run(race())
    assert sum(result[0] == "ok" for result in results) == 1
    assert sum(result[0:2] == ("http", 409) for result in results) == 1
    assert all(not (result[0] == "db" and result[1] == "40P01") for result in results)
    assert _org_operational_count(seed.org_id) == 1
    states = [_branch_state(branch_id) for branch_id in seed.branch_ids]
    assert sum(state[1] for state in states) == 1
    assert sum(state[0] == "temporarily_closed" for state in states) == 1


def test_duplicate_claim_and_expired_reclaim_reject_stale_fence() -> None:
    _safe_database_topology()
    seed = _seed(1)
    outbox_id = _insert_ready_outbox(seed)
    _postpone_all_except(outbox_id)
    first_worker = uuid.uuid4()
    second_worker = uuid.uuid4()

    async def claim_race():
        return await asyncio.gather(
            _claim_events(first_worker),
            _claim_events(second_worker),
        )

    batches = asyncio.run(claim_race())
    claimed = [
        (worker_id, event)
        for worker_id, batch in zip((first_worker, second_worker), batches)
        for event in batch
        if event["outbox_id"] == outbox_id
    ]
    assert len(claimed) == 1
    stale_worker, stale_event = claimed[0]
    assert int(stale_event["lease_fence"]) == 1
    assert _outbox_state(outbox_id)[0:3] == ("processing", 1, 1)

    _expire_lease(outbox_id)
    replacement_worker = uuid.uuid4()
    replacement_event = asyncio.run(_claim_specific(outbox_id, replacement_worker))
    assert int(replacement_event["lease_fence"]) == 2
    assert int(replacement_event["attempt_count"]) == 1

    with pytest.raises(RuntimeError, match="Lost lifecycle outbox lease before delivery"):
        asyncio.run(_deliver_with_fence(stale_event, stale_worker))

    asyncio.run(_deliver_with_fence(replacement_event, replacement_worker))
    status, fence, attempts, leased_by = _outbox_state(outbox_id)
    assert (status, fence, attempts, leased_by) == ("delivered", 2, 1, None)


def test_api_waits_for_transaction_b_then_revalidates_stable_state() -> None:
    _safe_database_topology()
    seed = _seed(2)
    branch_id = seed.branch_ids[0]
    correlation_id = asyncio.run(_transition(seed, branch_id, "temporarily_closed"))
    parent_id = _find_correlated(correlation_id, "branch.lifecycle_saga")
    worker_id = uuid.uuid4()
    parent_event = asyncio.run(_claim_specific(parent_id, worker_id))
    _install_transaction_b_barrier(branch_id)

    async def scenario():
        worker_task = asyncio.create_task(_process_saga_event(parent_event, worker_id))
        await asyncio.to_thread(_wait_for_sleeping_worker)
        api_task = asyncio.create_task(_attempt_transition(seed, branch_id, "active"))
        await asyncio.sleep(0.25)
        assert not api_task.done(), "API transition bypassed the Transaction-B row lock"
        worker_outcome = await worker_task
        api_outcome = await api_task
        return worker_outcome, api_outcome

    try:
        worker_outcome, api_outcome = asyncio.run(scenario())
    finally:
        _drop_barriers()

    assert worker_outcome == "delivered"
    assert api_outcome[0] == "ok"
    assert _outbox_state(parent_id)[0] == "delivered"
    assert _branch_state(branch_id) == ("active", True, True)


def test_lifecycle_to_finance_handoff_is_invisible_until_commit() -> None:
    _safe_database_topology()
    seed = _seed(2)
    branch_id = seed.branch_ids[0]
    correlation_id = asyncio.run(_transition(seed, branch_id, "temporarily_closed"))
    parent_id = _find_correlated(correlation_id, "branch.lifecycle_saga")
    worker_id = uuid.uuid4()
    parent_event = asyncio.run(_claim_specific(parent_id, worker_id))
    before_finance = _finance_counts()
    _install_refund_visibility_barrier(branch_id)

    async def scenario():
        task = asyncio.create_task(_process_saga_event(parent_event, worker_id))
        await asyncio.to_thread(_wait_for_sleeping_worker)
        invisible_count = await asyncio.to_thread(
            _outbox_count,
            correlation_id=correlation_id,
            event_type="branch.refund_required",
        )
        assert invisible_count == 0
        return await task

    try:
        outcome = asyncio.run(scenario())
    finally:
        _drop_barriers()

    assert outcome == "delivered"
    assert _outbox_count(correlation_id=correlation_id, event_type="branch.refund_required") == 1
    refund_id = _find_correlated(correlation_id, "branch.refund_required")
    refund_worker = uuid.uuid4()
    refund_event = asyncio.run(_claim_specific(refund_id, refund_worker))
    refund_outcome = asyncio.run(_process_refund_required_event(refund_event, refund_worker))
    assert refund_outcome == "delivered"
    assert _outbox_state(refund_id)[0] == "delivered"
    assert _finance_counts() == before_finance


def test_deadlock_detector_canary_observes_exactly_one_40p01_victim() -> None:
    _safe_database_topology()
    rendezvous = threading.Barrier(2)
    outcomes: list[str] = []
    failures: list[str] = []

    def contender(first_key: int, second_key: int) -> None:
        connection = _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout='8s'")
                cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (first_key,))
                rendezvous.wait(timeout=5)
                try:
                    cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (second_key,))
                except DeadlockDetected as exc:
                    outcomes.append(str(exc.sqlstate))
                    connection.rollback()
                    return
                connection.commit()
                outcomes.append("acquired")
        except Exception as exc:
            failures.append(repr(exc))
            connection.rollback()
        finally:
            connection.close()

    first = threading.Thread(target=contender, args=(910001, 910002), daemon=True)
    second = threading.Thread(target=contender, args=(910002, 910001), daemon=True)
    first.start()
    second.start()
    first.join(timeout=12)
    second.join(timeout=12)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert sorted(outcomes) == ["40P01", "acquired"]
