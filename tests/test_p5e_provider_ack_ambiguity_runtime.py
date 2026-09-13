from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy.engine import make_url

from scripts.ci.p5e_provider_ack_worker import (
    initialize_store,
    seed_search_document,
)


ROOT = Path(__file__).resolve().parents[1]
_ADMIN_LOGIN = "migration_owner"
_AUTH_LOGIN = "auth_p5e_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_DATABASE = "gymflow_p5e_test"

_PROVIDER_CAPABILITIES = (
    (
        "app_secure.claim_branch_search_projection(uuid,uuid)",
        "app_secure.claim_branch_search_projection(uuid,uuid,bigint)",
    ),
    (
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text)",
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,bigint,text,text,text,text,text,bigint,text,text)",
    ),
    (
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,text,text,text,text,text)",
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,bigint,text,text,text,text,text)",
    ),
    (
        "app_secure.repair_branch_search_provider_drift(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text,text)",
        "app_secure.repair_branch_search_provider_drift(uuid,uuid,bigint,bigint,text,text,text,text,text,bigint,text,text,text)",
    ),
    (
        "app_secure.materialize_branch_member_notifications(uuid,uuid)",
        "app_secure.materialize_branch_member_notifications(uuid,uuid,bigint)",
    ),
    (
        "app_secure.claim_notification_delivery_v2(uuid,uuid)",
        "app_secure.claim_notification_delivery_v2(uuid,uuid,bigint)",
    ),
    (
        "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,text,text,text)",
        "app_secure.acknowledge_notification_provider_acceptance(uuid,uuid,bigint,text,text,text)",
    ),
    (
        "app_secure.record_notification_delivery_failure(uuid,uuid,text,text,text)",
        "app_secure.record_notification_delivery_failure(uuid,uuid,bigint,text,text,text)",
    ),
    (
        "app_secure.claim_notification_reconciliation(uuid,uuid)",
        "app_secure.claim_notification_reconciliation(uuid,uuid,bigint)",
    ),
    (
        "app_secure.complete_notification_reconciliation(uuid,uuid,text,text)",
        "app_secure.complete_notification_reconciliation(uuid,uuid,bigint,text,text)",
    ),
    (
        "app_secure.record_notification_reconciliation_failure(uuid,uuid,text,boolean)",
        "app_secure.record_notification_reconciliation_failure(uuid,uuid,bigint,text,boolean)",
    ),
)


@dataclass(frozen=True)
class _BaseSeed:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    branch_id: uuid.UUID


@dataclass(frozen=True)
class _SearchSeed:
    base: _BaseSeed
    event_id: uuid.UUID
    operation: str
    desired_version: int


@dataclass(frozen=True)
class _NotificationSeed:
    base: _BaseSeed
    parent_id: uuid.UUID
    command_id: uuid.UUID
    member_id: uuid.UUID
    idempotency_key: str


def _required_url(name: str):
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"P5-E runtime requires {name}")
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(f"P5-E {name} must include host, port and database")
    return url


def _safe_database_topology() -> tuple[str, int, str]:
    if os.environ.get("P5E_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-E acknowledgement faults require explicit CI enablement")
    runtime = _required_url("TEST_DATABASE_URL")
    admin = _required_url("TEST_ADMIN_DATABASE_URL")
    runtime_topology = (str(runtime.host), int(runtime.port), str(runtime.database))
    admin_topology = (str(admin.host), int(admin.port), str(admin.database))
    if runtime_topology != admin_topology:
        raise RuntimeError("P5-E runtime/admin URLs must target one disposable topology")
    if runtime_topology[0] not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P5-E database host: {runtime_topology[0]}")
    if runtime_topology[2] != _DATABASE:
        raise RuntimeError(f"unsafe P5-E database: {runtime_topology[2]}")
    if os.environ.get("P5E_DISPOSABLE_DATABASE") != _DATABASE:
        raise RuntimeError("P5-E disposable database acknowledgement is absent")
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


def _as_security_owner(cursor) -> None:
    cursor.execute("SET LOCAL ROLE app_security_owner")


def _install_worker_context(
    cursor,
    *,
    org_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> None:
    cursor.execute(
        """
        SELECT
            pg_catalog.set_config('app.current_org_id',%s,true),
            pg_catalog.set_config('app.current_role','branch_lifecycle_worker',true),
            pg_catalog.set_config('app.internal_maintenance','branch_lifecycle_saga',true),
            pg_catalog.set_config('app.worker_id',%s,true),
            pg_catalog.set_config('app.request_id',%s,true)
        """,
        (str(org_id), str(worker_id), str(uuid.uuid4())),
    )


def _insert_canonical_initial_branch_state(seed: _BaseSeed) -> None:
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
                    %s,%s,'active',true,true,true,'active',true,NULL,NULL,
                    'api',NULL,NULL,false,NULL,NULL,NULL,0,1,NULL,NULL,NULL,
                    NULL,NULL,NULL,NULL,NULL,1,0,%s,NULL,NULL,NULL
                )
                RETURNING status_changed_at,updated_at
                """,
                (seed.branch_id, seed.org_id, uuid.uuid4().hex[:26].upper()),
            )
            returned = cursor.fetchone()
            assert returned is not None
            assert all(value is not None for value in returned)
        connection.commit()


def _seed_base() -> _BaseSeed:
    seed = _BaseSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code,
                    website_verified,social_links,verification_status,country,
                    profile_completed
                ) VALUES (
                    %s,'P5-E Runtime Org',%s,'basic',true,10,'INR',
                    false,'{}'::jsonb,'pending','India',false
                )
                """,
                (seed.org_id, f"p5e-runtime-{seed.org_id.hex}"),
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
                    %s,%s,'P5-E Runtime Owner',%s,
                    'fixture-not-a-real-password-hash',true,true
                )
                """,
                (
                    seed.owner_id,
                    seed.org_id,
                    f"p5e-{seed.owner_id.hex}@example.test",
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    timezone,currency_code,region_code,country_code
                ) VALUES (
                    %s,%s,'P5-E Runtime Branch',%s,%s,
                    'Asia/Kolkata','INR','TN','IN'
                )
                """,
                (
                    seed.branch_id,
                    seed.org_id,
                    f"P5E-{seed.branch_id.hex[:8]}",
                    f"p5e-{seed.branch_id.hex}",
                ),
            )
        connection.commit()
    _insert_canonical_initial_branch_state(seed)
    return seed


def _visible_document(seed: _BaseSeed) -> dict[str, Any]:
    return {
        "branch_id": str(seed.branch_id),
        "organization_id": str(seed.org_id),
        "name": "P5-E Runtime Branch",
        "slug": f"p5e-{seed.branch_id.hex}",
        "timezone": "Asia/Kolkata",
        "region_code": "TN",
        "country_code": "IN",
        "status": "active",
        "is_operational": True,
        "is_public": True,
        "search_version": 1,
    }


def _seed_search(operation: str, store: Path) -> _SearchSeed:
    if operation not in {"index", "delete"}:
        raise ValueError(f"unsupported P5-E search operation: {operation}")
    base = _seed_base()
    event_id = uuid.uuid4()
    desired_version = 1 if operation == "index" else 2
    event_type = "branch.search_index" if operation == "index" else "branch.search_deindex"

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            if operation == "delete":
                cursor.execute(
                    """
                    UPDATE public.org_branch_state
                    SET is_public=false,
                        search_visibility_version=2,
                        search_provider_ack_version=1,
                        search_provider_document_hash=repeat('1',64),
                        search_provider_evidence_sha256=repeat('2',64),
                        search_provider_code='opensearch',
                        search_provider_index='branches-v1',
                        search_provider_document_id=%s,
                        search_provider_acknowledged_at=pg_catalog.clock_timestamp(),
                        search_provider_reconciled_at=pg_catalog.clock_timestamp(),
                        search_last_synced_at=pg_catalog.clock_timestamp()
                    WHERE branch_id=%s AND org_id=%s
                    """,
                    (str(base.branch_id), base.branch_id, base.org_id),
                )
                assert cursor.rowcount == 1
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (%s,%s,%s,%s,'{}'::jsonb,5,%s)
                """,
                (
                    event_id,
                    base.org_id,
                    base.branch_id,
                    event_type,
                    uuid.uuid4(),
                ),
            )
        connection.commit()

    if operation == "delete":
        seed_search_document(
            store,
            document_id=str(base.branch_id),
            provider_version=1,
            document=_visible_document(base),
        )
    else:
        initialize_store(store)
    return _SearchSeed(base, event_id, operation, desired_version)


def _seed_notification() -> _NotificationSeed:
    base = _seed_base()
    parent_id = uuid.uuid4()
    command_id = uuid.uuid4()
    member_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    idempotency_key = (
        f"branch-lifecycle/{correlation_id}/{member_id}/email"
    )

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(base.org_id),),
            )
            cursor.execute(
                """
                INSERT INTO public.members(
                    id,org_id,home_branch_id,member_uid,member_number,
                    name,email,status,is_active,is_migrated,created_at,updated_at
                ) VALUES (
                    %s,%s,%s,%s,1,'P5-E Member','p5e-member@example.test',
                    'active',true,false,pg_catalog.clock_timestamp(),
                    pg_catalog.clock_timestamp()
                )
                """,
                (
                    member_id,
                    base.org_id,
                    base.branch_id,
                    f"P5E{member_id.hex[:20].upper()}",
                ),
            )
            _as_security_owner(cursor)
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    status,attempt_count,max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'branch.member_notification','{}'::jsonb,
                    'superseded',1,5,%s
                )
                """,
                (parent_id, base.org_id, base.branch_id, correlation_id),
            )
            cursor.execute(
                """
                INSERT INTO public.notification_commands(
                    command_id,source_outbox_id,tenant_id,branch_id,member_id,
                    effect_type,channel,template_key,template_data,
                    idempotency_key,status,attempt_count,max_attempts,
                    next_attempt_at,correlation_id
                ) VALUES (
                    %s,%s,%s,%s,%s,'branch.member_notification','email',
                    'branch_lifecycle_status_changed',
                    '{"branch_name":"P5-E Runtime Branch","from_status":"active",'
                    '"to_status":"temporarily_closed"}'::jsonb,
                    %s,'pending',0,5,pg_catalog.clock_timestamp(),%s
                )
                """,
                (
                    command_id,
                    parent_id,
                    base.org_id,
                    base.branch_id,
                    member_id,
                    idempotency_key,
                    correlation_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'notification.delivery',
                    pg_catalog.jsonb_build_object('command_id',%s::text),5,%s
                )
                """,
                (
                    command_id,
                    base.org_id,
                    base.branch_id,
                    command_id,
                    correlation_id,
                ),
            )
        connection.commit()
    return _NotificationSeed(
        base,
        parent_id,
        command_id,
        member_id,
        idempotency_key,
    )


def _install_fault_triggers() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION app_secure.p5e_reject_search_ack()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RAISE EXCEPTION 'P5-E injected search acknowledgement failure'
                        USING ERRCODE='08006';
                END;
                $function$;

                CREATE OR REPLACE FUNCTION app_secure.p5e_reject_notification_ack()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RAISE EXCEPTION 'P5-E injected notification acknowledgement failure'
                        USING ERRCODE='08006';
                END;
                $function$;

                REVOKE ALL ON FUNCTION
                    app_secure.p5e_reject_search_ack(),
                    app_secure.p5e_reject_notification_ack()
                FROM PUBLIC;
                GRANT USAGE ON SCHEMA app_secure TO migration_owner;
                GRANT EXECUTE ON FUNCTION
                    app_secure.p5e_reject_search_ack(),
                    app_secure.p5e_reject_notification_ack()
                TO migration_owner;
                """
            )
            cursor.execute("RESET ROLE")
            cursor.execute(
                """
                DROP TRIGGER IF EXISTS p5e_reject_search_ack
                    ON public.org_branch_state;
                CREATE TRIGGER p5e_reject_search_ack
                    BEFORE UPDATE OF search_provider_ack_version
                    ON public.org_branch_state
                    FOR EACH ROW
                    WHEN (
                        NEW.search_provider_ack_version
                        IS DISTINCT FROM OLD.search_provider_ack_version
                    )
                    EXECUTE FUNCTION app_secure.p5e_reject_search_ack();
                ALTER TABLE public.org_branch_state
                    DISABLE TRIGGER p5e_reject_search_ack;

                DROP TRIGGER IF EXISTS p5e_reject_notification_ack
                    ON public.notification_commands;
                CREATE TRIGGER p5e_reject_notification_ack
                    BEFORE UPDATE OF status
                    ON public.notification_commands
                    FOR EACH ROW
                    WHEN (
                        OLD.status='processing'
                        AND NEW.status='provider_accepted'
                    )
                    EXECUTE FUNCTION app_secure.p5e_reject_notification_ack();
                ALTER TABLE public.notification_commands
                    DISABLE TRIGGER p5e_reject_notification_ack;
                """
            )
            _as_security_owner(cursor)
            cursor.execute(
                """
                REVOKE EXECUTE ON FUNCTION
                    app_secure.p5e_reject_search_ack(),
                    app_secure.p5e_reject_notification_ack()
                FROM migration_owner;
                REVOKE USAGE ON SCHEMA app_secure FROM migration_owner;
                """
            )
            cursor.execute("RESET ROLE")
        connection.commit()


def _drop_fault_triggers() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DROP TRIGGER IF EXISTS p5e_reject_search_ack
                    ON public.org_branch_state;
                DROP TRIGGER IF EXISTS p5e_reject_notification_ack
                    ON public.notification_commands;
                """
            )
            _as_security_owner(cursor)
            cursor.execute(
                """
                DROP FUNCTION IF EXISTS app_secure.p5e_reject_search_ack();
                DROP FUNCTION IF EXISTS app_secure.p5e_reject_notification_ack();
                """
            )
        connection.commit()


def _set_fault_trigger(surface: str, *, enabled: bool) -> None:
    targets = {
        "search": ("public.org_branch_state", "p5e_reject_search_ack"),
        "notification": (
            "public.notification_commands",
            "p5e_reject_notification_ack",
        ),
    }
    relation, trigger = targets[surface]
    action = "ENABLE" if enabled else "DISABLE"
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {relation} {action} TRIGGER {trigger}")
        connection.commit()


def _run_worker(store: Path) -> dict[str, int]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "",
            "AUTH_DATABASE_URL": "",
            "WORKER_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
            "MAINTENANCE_DATABASE_URL": "",
            "FINANCE_CONFIG_DATABASE_URL": "",
            "ENVIRONMENT": "production",
            "DOERS_PROCESS_PROFILE": "worker",
            "CELERY_WORKER_PROFILE": "worker",
            "SEARCH_PROVIDER_MODE": "disabled",
            "NOTIFICATION_EMAIL_PROVIDER_MODE": "disabled",
            "P4C_RESEND_API_KEY": "",
            "P5E_PROCESS_FAULTS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-s",
            "-m",
            "scripts.ci.p5e_provider_ack_worker",
            "--store",
            str(store),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"P5-E replacement worker failed:\n{completed.stdout}")
    records = []
    for line in completed.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("marker") == "P5E_WORKER_RESULT":
            records.append(record)
    assert len(records) == 1, completed.stdout
    summary = records[0]["summary"]
    assert isinstance(summary, dict)
    return {str(key): int(value) for key, value in summary.items()}


def _admin_row(org_id: uuid.UUID, statement: str, parameters: tuple[Any, ...]):
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(org_id),),
            )
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
            assert row is not None
            return tuple(row)


def _provider_search_row(store: Path, branch_id: uuid.UUID) -> tuple[Any, ...]:
    with sqlite3.connect(store) as connection:
        row = connection.execute(
            """
            SELECT provider_version,document_json,deleted,effect_count,mutation_calls
            FROM search_effects WHERE logical_key=?
            """,
            (f"branches-v1/{branch_id}",),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _provider_notification_row(store: Path, key: str) -> tuple[Any, ...]:
    with sqlite3.connect(store) as connection:
        row = connection.execute(
            """
            SELECT reference_id,request_sha256,effect_count,send_calls
            FROM notification_effects WHERE idempotency_key=?
            """,
            (key,),
        ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.fixture(scope="session", autouse=True)
def _disposable_runtime() -> Iterator[None]:
    _safe_database_topology()
    _install_fault_triggers()
    yield
    _drop_fault_triggers()


@pytest.mark.parametrize("operation", ["index", "delete"])
def test_search_provider_success_ack_failure_replays_same_version_once(
    operation: str,
    tmp_path: Path,
) -> None:
    store = tmp_path / f"p5e-search-{operation}.sqlite3"
    seed = _seed_search(operation, store)
    _set_fault_trigger("search", enabled=True)
    first = _run_worker(store)
    assert first["claimed"] == 1
    assert first["retry"] == 1

    interim_outbox = _admin_row(
        seed.base.org_id,
        """
        SELECT status,attempt_count,lease_fence,leased_by IS NULL,
               last_error LIKE '%%P5-E injected search acknowledgement failure%%'
        FROM public.branch_outbox_events WHERE outbox_id=%s
        """,
        (seed.event_id,),
    )
    assert interim_outbox == ("pending", 1, 1, True, True)
    interim_state = _admin_row(
        seed.base.org_id,
        """
        SELECT search_provider_ack_version,
               (SELECT count(*) FROM public.branch_search_effect_attempts
                WHERE outbox_id=%s)
        FROM public.org_branch_state WHERE branch_id=%s AND org_id=%s
        """,
        (seed.event_id, seed.base.branch_id, seed.base.org_id),
    )
    expected_previous = None if operation == "index" else 1
    expected_deleted = 0 if operation == "index" else 1
    assert interim_state == (expected_previous, 0)
    provider_after_failure = _provider_search_row(store, seed.base.branch_id)
    assert provider_after_failure[0] == seed.desired_version
    assert provider_after_failure[2:] == (expected_deleted, 1, 1)

    _set_fault_trigger("search", enabled=False)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            cursor.execute(
                """
                UPDATE public.branch_outbox_events
                SET process_after=pg_catalog.clock_timestamp()
                WHERE outbox_id=%s AND status='pending'
                """,
                (seed.event_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()

    second = _run_worker(store)
    assert second["claimed"] == 1
    assert second["delivered"] == 1
    final_outbox = _admin_row(
        seed.base.org_id,
        """
        SELECT status,attempt_count,lease_fence,leased_by IS NULL,last_error
        FROM public.branch_outbox_events WHERE outbox_id=%s
        """,
        (seed.event_id,),
    )
    assert final_outbox == ("delivered", 2, 2, True, None)
    final_state = _admin_row(
        seed.base.org_id,
        """
        SELECT search_provider_ack_version,search_provider_code,
               search_provider_index,search_provider_document_id,
               (SELECT count(*) FROM public.branch_search_effect_attempts
                WHERE outbox_id=%s),
               (SELECT min(attempt_number) FROM public.branch_search_effect_attempts
                WHERE outbox_id=%s)
        FROM public.org_branch_state WHERE branch_id=%s AND org_id=%s
        """,
        (
            seed.event_id,
            seed.event_id,
            seed.base.branch_id,
            seed.base.org_id,
        ),
    )
    assert final_state == (
        seed.desired_version,
        "opensearch",
        "branches-v1",
        str(seed.base.branch_id),
        1,
        2,
    )
    provider_final = _provider_search_row(store, seed.base.branch_id)
    assert provider_final[0] == seed.desired_version
    assert provider_final[2:] == (expected_deleted, 1, 2)
    if operation == "index":
        assert json.loads(str(provider_final[1])) == _visible_document(seed.base)
    else:
        assert provider_final[1] is None


def test_notification_provider_acceptance_ack_failure_reclaims_same_key_once(
    tmp_path: Path,
) -> None:
    store = tmp_path / "p5e-notification.sqlite3"
    initialize_store(store)
    seed = _seed_notification()
    _set_fault_trigger("notification", enabled=True)
    first = _run_worker(store)
    assert first["claimed"] == 1
    assert first["lease_lost"] == 1

    interim = _admin_row(
        seed.base.org_id,
        """
        SELECT o.status,o.attempt_count,o.lease_fence,o.leased_by IS NOT NULL,
               c.status,c.attempt_count,c.leased_by IS NOT NULL,
               c.idempotency_key,c.provider_reference_id,
               (SELECT count(*) FROM public.notification_delivery_attempts a
                WHERE a.command_id=c.command_id)
        FROM public.branch_outbox_events o
        JOIN public.notification_commands c ON c.command_id=o.outbox_id
        WHERE o.outbox_id=%s
        """,
        (seed.command_id,),
    )
    assert interim == (
        "processing",
        1,
        1,
        True,
        "processing",
        1,
        True,
        seed.idempotency_key,
        None,
        0,
    )
    provider_after_failure = _provider_notification_row(
        store,
        seed.idempotency_key,
    )
    assert provider_after_failure[2:] == (1, 1)

    _set_fault_trigger("notification", enabled=False)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            cursor.execute(
                """
                UPDATE public.branch_outbox_events
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE outbox_id=%s AND status='processing'
                """,
                (seed.command_id,),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                UPDATE public.notification_commands
                SET leased_until=pg_catalog.clock_timestamp()-INTERVAL '1 second'
                WHERE command_id=%s AND status='processing'
                """,
                (seed.command_id,),
            )
            assert cursor.rowcount == 1
        connection.commit()

    second = _run_worker(store)
    assert second["claimed"] == 1
    assert second["provider_accepted"] == 1
    provider_final = _provider_notification_row(store, seed.idempotency_key)
    assert provider_final[0] == provider_after_failure[0]
    assert provider_final[1] == provider_after_failure[1]
    assert provider_final[2:] == (1, 2)

    final = _admin_row(
        seed.base.org_id,
        """
        SELECT o.status,o.attempt_count,o.lease_fence,o.leased_by IS NULL,
               c.status,c.attempt_count,c.leased_by IS NULL,
               c.idempotency_key,c.provider_reference_id,
               c.acknowledged_at IS NOT NULL,c.completed_at IS NULL,
               array_agg(a.attempt_number ORDER BY a.attempt_number),
               array_agg(a.outcome ORDER BY a.attempt_number)
        FROM public.branch_outbox_events o
        JOIN public.notification_commands c ON c.command_id=o.outbox_id
        JOIN public.notification_delivery_attempts a
          ON a.command_id=c.command_id
        WHERE o.outbox_id=%s
        GROUP BY o.status,o.attempt_count,o.lease_fence,o.leased_by,
                 c.status,c.attempt_count,c.leased_by,c.idempotency_key,
                 c.provider_reference_id,c.acknowledged_at,c.completed_at
        """,
        (seed.command_id,),
    )
    assert final == (
        "provider_accepted",
        1,
        2,
        True,
        "provider_accepted",
        2,
        True,
        seed.idempotency_key,
        provider_final[0],
        True,
        True,
        [1, 2],
        ["ambiguous_outcome", "provider_accepted_nonterminal"],
    )


def test_provider_capabilities_reject_same_worker_aba_fence(tmp_path: Path) -> None:
    worker_id = uuid.uuid4()
    search = _seed_search("index", tmp_path / "p5e-capability-fence.sqlite3")
    notification = _seed_notification()
    reconciliation_id = uuid.uuid4()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as connection:
        with connection.cursor() as cursor:
            _as_security_owner(cursor)
            cursor.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    max_attempts,correlation_id
                ) VALUES (
                    %s,%s,%s,'notification.reconcile','{}'::jsonb,5,%s
                )
                """,
                (
                    reconciliation_id,
                    notification.base.org_id,
                    notification.base.branch_id,
                    uuid.uuid4(),
                ),
            )
            cursor.execute(
                """
                UPDATE public.branch_outbox_events
                SET status='processing',attempt_count=1,lease_fence=2,
                    leased_by=%s,
                    leased_until=pg_catalog.clock_timestamp()+INTERVAL '5 minutes',
                    claimed_at=pg_catalog.clock_timestamp(),
                    processed_at=NULL,last_error=NULL
                WHERE outbox_id=ANY(%s::uuid[])
                """,
                (
                    worker_id,
                    [
                        search.event_id,
                        notification.parent_id,
                        notification.command_id,
                        reconciliation_id,
                    ],
                ),
            )
            assert cursor.rowcount == 4
        connection.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as connection:
        with connection.cursor() as cursor:
            for old_signature, new_signature in _PROVIDER_CAPABILITIES:
                cursor.execute(
                    """
                    SELECT
                        pg_catalog.has_function_privilege(
                            current_user,%s,'EXECUTE'
                        ),
                        pg_catalog.has_function_privilege(
                            current_user,%s,'EXECUTE'
                        ),
                        COALESCE((
                            SELECT pg_catalog.bool_or(
                                acl_data.grantee = 0
                                AND acl_data.privilege_type = 'EXECUTE'
                            )
                            FROM pg_catalog.pg_proc AS proc_data
                            CROSS JOIN LATERAL pg_catalog.aclexplode(
                                COALESCE(
                                    proc_data.proacl,
                                    pg_catalog.acldefault(
                                        'f', proc_data.proowner
                                    )
                                )
                            ) AS acl_data
                            WHERE proc_data.oid =
                                pg_catalog.to_regprocedure(%s)
                        ), FALSE)
                    """,
                    (old_signature, new_signature, new_signature),
                )
                old_execute, new_execute, public_execute = cursor.fetchone()
                assert not old_execute, (
                    f"worker retained unfenced provider capability: {old_signature}"
                )
                assert new_execute, (
                    f"worker lacks fenced provider capability: {new_signature}"
                )
                assert not public_execute
            connection.commit()

        stale_calls = (
            (
                search.base.org_id,
                "SELECT * FROM app_secure.claim_branch_search_projection(%s,%s,%s)",
                (search.event_id, worker_id, 1),
            ),
            (
                notification.base.org_id,
                "SELECT app_secure.materialize_branch_member_notifications(%s,%s,%s)",
                (notification.parent_id, worker_id, 1),
            ),
            (
                notification.base.org_id,
                "SELECT * FROM app_secure.claim_notification_delivery_v2(%s,%s,%s)",
                (notification.command_id, worker_id, 1),
            ),
            (
                notification.base.org_id,
                "SELECT * FROM app_secure.claim_notification_reconciliation(%s,%s,%s)",
                (reconciliation_id, worker_id, 1),
            ),
        )
        for org_id, statement, parameters in stale_calls:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        _install_worker_context(
                            cursor,
                            org_id=org_id,
                            worker_id=worker_id,
                        )
                        cursor.execute(statement, parameters)
            except InsufficientPrivilege as exc:
                assert exc.sqlstate == "42501"
                assert "current live claim fence" in str(exc)
            else:
                raise AssertionError(
                    "stale provider capability unexpectedly succeeded"
                )

        with connection.transaction():
            with connection.cursor() as cursor:
                _install_worker_context(
                    cursor,
                    org_id=search.base.org_id,
                    worker_id=worker_id,
                )
                cursor.execute(
                    """
                    SELECT operation,desired_version
                    FROM app_secure.claim_branch_search_projection(%s,%s,%s)
                    """,
                    (search.event_id, worker_id, 2),
                )
                assert cursor.fetchone() == ("index", 1)
