from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest
from sqlalchemy.engine import make_url


_DATABASE_ENV = "TEST_DATABASE_URL"
_ADMIN_DATABASE_ENV = "TEST_ADMIN_DATABASE_URL"
_ADMIN_LOGIN = "migration_owner"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"


def _required_url(name: str):
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"P5-W runtime requires {name}; fixed database defaults are forbidden")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-W {name} must include host, port and database")
    return url


def _topology(name: str) -> tuple[str, int, str]:
    url = _required_url(name)
    return str(url.host), int(url.port), str(url.database)


def _safe_topology() -> tuple[str, int, str]:
    runtime = _topology(_DATABASE_ENV)
    admin = _topology(_ADMIN_DATABASE_ENV)
    if runtime != admin:
        raise RuntimeError(
            "P5-W runtime/admin URLs must target one disposable topology: "
            f"runtime={runtime!r} admin={admin!r}"
        )
    host, _port, database = runtime
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-W runtime host: {host}")
    if "test" not in database or database in {
        "gymflow",
        "gymflow_test",
        "gymflow_migration_test",
        "production",
    }:
        raise RuntimeError(f"unsafe P5-W runtime database: {database}")
    return runtime


def _connect(login: str, password_env: str):
    host, port, database = _safe_topology()
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=login,
        password=os.environ[password_env],
    )


def _seed_two_final_attempt_jobs() -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    lifecycle_id = uuid.uuid4()
    lifecycle_correlation = uuid.uuid4()
    branch_hours_correlation = uuid.uuid4()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,profile_completed
                ) VALUES (
                    %s,'P5-W Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (org_id, f"p5w-runtime-{org_id.hex}"),
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
                    %s,%s,'P5-W Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (
                    owner_id,
                    org_id,
                    f"p5w-runtime-{owner_id.hex}@example.test",
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    timezone,currency_code,region_code,country_code
                ) VALUES (
                    %s,%s,'P5-W Runtime Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    branch_id,
                    org_id,
                    f"P5W-{branch_id.hex[:8]}",
                    f"p5w-{branch_id.hex}",
                ),
            )
        connection.commit()

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
                (str(org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (%s,%s,%s,'branch.lifecycle_saga','{}'::jsonb,1,%s)
                """,
                (lifecycle_id, org_id, branch_id, lifecycle_correlation),
            )
            # The lifecycle queue has a bounded saga-orchestrator write path,
            # while branch-hours enqueue is intentionally restricted to a
            # canonical, database-revalidated principal. Switch to the seeded
            # active owner rather than weakening the production RLS policy for
            # this fault-injection fixture.
            cursor.execute(
                """
                SELECT
                    pg_catalog.set_config('app.current_role','owner',true),
                    pg_catalog.set_config('app.current_user_id',%s,true),
                    pg_catalog.set_config('app.current_principal_type','owner',true)
                """,
                (str(owner_id),),
            )
            cursor.execute(
                "SELECT public.enqueue_branch_hours_rebuild(%s,%s)",
                (branch_id, branch_hours_correlation),
            )
            transactional_id = cursor.fetchone()[0]
        connection.commit()

    # Put the branch-hours command at its last available new-attempt boundary.
    # Its first P5 claim reaches 15; reclaim must not increment beyond that.
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.transactional_outbox
                SET delivery_attempts=14
                WHERE id=%s
                  AND leased_by IS NULL
                  AND processed_at IS NULL
                  AND dead_lettered_at IS NULL
                """,
                (transactional_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()

    return lifecycle_id, transactional_id


def _expire_claim(relation: str, id_column: str, event_id: uuid.UUID) -> None:
    if (relation, id_column) not in {
        ("public.branch_outbox_events", "outbox_id"),
        ("public.transactional_outbox", "id"),
    }:
        raise ValueError("P5-W test attempted an unapproved relation")
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {relation}
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE {id_column}=%s AND leased_by IS NOT NULL
                """,
                (event_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()


async def _exercise_lifecycle_aba(worker_id: uuid.UUID, event_id: uuid.UUID) -> None:
    from app.core.database import worker_async_session_maker
    from app.tasks.branch_outbox_poller import _claim_events, _fail_event, _mark_delivered

    first = {event["outbox_id"]: event for event in await _claim_events(worker_id)}[event_id]
    assert int(first["attempt_count"]) == 1
    assert int(first["lease_fence"]) == 1

    _expire_claim("public.branch_outbox_events", "outbox_id", event_id)
    second = {event["outbox_id"]: event for event in await _claim_events(worker_id)}[event_id]
    assert int(second["attempt_count"]) == 1
    assert int(second["lease_fence"]) == 2

    async with worker_async_session_maker() as stale_session:
        with pytest.raises(RuntimeError, match="Lost lifecycle outbox lease"):
            await _mark_delivered(
                stale_session,
                outbox_id=event_id,
                worker_id=worker_id,
                lease_fence=int(first["lease_fence"]),
            )
        await stale_session.rollback()

    stale_failure = await _fail_event(
        first,
        worker_id,
        RuntimeError("stale permanent P5-W failure replay"),
        permanent=True,
    )
    assert stale_failure == "lease_lost"

    async with worker_async_session_maker() as current_session:
        await _mark_delivered(
            current_session,
            outbox_id=event_id,
            worker_id=worker_id,
            lease_fence=int(second["lease_fence"]),
        )
        await current_session.commit()


async def _exercise_transactional_aba(worker_id: uuid.UUID, event_id: uuid.UUID) -> None:
    from app.core.database import worker_async_session_maker
    from app.tasks.outbox_poller import (
        _claim_ready_events,
        _complete_owned_event,
        _release_failed_event,
    )

    first = {event["id"]: event for event in await _claim_ready_events(worker_id)}[event_id]
    assert int(first["delivery_attempts"]) == 15
    assert int(first["lease_fence"]) == 1

    _expire_claim("public.transactional_outbox", "id", event_id)
    second = {event["id"]: event for event in await _claim_ready_events(worker_id)}[event_id]
    assert int(second["delivery_attempts"]) == 15
    assert int(second["lease_fence"]) == 2

    async with worker_async_session_maker() as stale_session:
        with pytest.raises(RuntimeError, match="Lost branch-hours outbox lease"):
            await _complete_owned_event(
                stale_session,
                event_id=event_id,
                worker_id=worker_id,
                lease_fence=int(first["lease_fence"]),
            )
        await stale_session.rollback()

    stale_failure = await _release_failed_event(
        event=first,
        worker_id=worker_id,
        error=RuntimeError("stale P5-W failure replay"),
        permanent=False,
    )
    assert stale_failure == "lease_lost"

    async with worker_async_session_maker() as current_session:
        await _complete_owned_event(
            current_session,
            event_id=event_id,
            worker_id=worker_id,
            lease_fence=int(second["lease_fence"]),
        )
        await current_session.commit()


def test_reclaim_rotates_fence_rejects_aba_and_recovers_final_attempts() -> None:
    _safe_topology()
    lifecycle_id, transactional_id = _seed_two_final_attempt_jobs()
    reused_worker_id = uuid.uuid4()

    asyncio.run(_exercise_lifecycle_aba(reused_worker_id, lifecycle_id))
    asyncio.run(_exercise_transactional_aba(reused_worker_id, transactional_id))

    # Both queue tables retain FORCE ROW LEVEL SECURITY. Observe the terminal
    # rows through the dedicated worker identity, whose queue SELECT policies
    # are intentionally cross-tenant, instead of relying on table-owner bypass.
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,lease_fence,leased_by,leased_until
                FROM public.branch_outbox_events WHERE outbox_id=%s
                """,
                (lifecycle_id,),
            )
            assert cursor.fetchone() == ("delivered", 1, 2, None, None)

            cursor.execute(
                """
                SELECT processed_at IS NOT NULL,dead_lettered_at,delivery_attempts,
                       lease_fence,leased_by,leased_until
                FROM public.transactional_outbox WHERE id=%s
                """,
                (transactional_id,),
            )
            assert cursor.fetchone() == (True, None, 15, 2, None, None)
        connection.commit()
