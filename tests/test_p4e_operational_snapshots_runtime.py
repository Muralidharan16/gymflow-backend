from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy.engine import make_url


_TOPOLOGY_URL_ENV = "TEST_DATABASE_URL"
_ADMIN_TOPOLOGY_URL_ENV = "TEST_ADMIN_DATABASE_URL"

_ADMIN_LOGIN = "migration_owner"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_MAINTENANCE_LOGIN = "lifecycle_maintenance_test_runtime"

_SEARCH_SNAPSHOT = "app_secure.search_operational_snapshot()"
_REFUND_SNAPSHOT = "app_secure.refund_execution_operational_snapshot()"

_SEARCH_COLUMNS = [
    "pending_count",
    "processing_count",
    "dead_letter_count",
    "reconciliation_candidate_count",
    "oldest_actionable_age_seconds",
]
_REFUND_COLUMNS = [
    "pending_count",
    "processing_count",
    "retry_pending_count",
    "provider_accepted_count",
    "reconciliation_pending_count",
    "dead_letter_count",
    "oldest_unresolved_age_seconds",
]


def _required_database_url(env_name: str):
    raw = os.environ.get(env_name)
    if not raw:
        raise RuntimeError(
            f"P4E operational snapshot runtime tests require {env_name}; "
            "fixed local database defaults are forbidden"
        )
    url = make_url(raw)
    if not url.host or not url.port or not url.database:
        raise RuntimeError(
            f"P4E operational snapshot {env_name} must include "
            "host, port, and database"
        )
    return url


def _runtime_topology() -> tuple[str, int, str]:
    url = _required_database_url(_TOPOLOGY_URL_ENV)
    return str(url.host), int(url.port), str(url.database)


def _admin_topology() -> tuple[str, int, str]:
    url = _required_database_url(_ADMIN_TOPOLOGY_URL_ENV)
    return str(url.host), int(url.port), str(url.database)


def _assert_same_disposable_topology() -> tuple[str, int, str]:
    runtime = _runtime_topology()
    admin = _admin_topology()
    if runtime != admin:
        raise RuntimeError(
            "P4E runtime/admin URLs must target the same disposable topology: "
            f"runtime={runtime!r}, admin={admin!r}"
        )
    host, _port, db_name = runtime
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P4E runtime host: {host}")
    if "test" not in db_name:
        raise RuntimeError(f"unsafe P4E runtime database: {db_name}")
    if db_name in {
        "gymflow",
        "gymflow_test",
        "gymflow_migration_test",
        "production",
    }:
        raise RuntimeError(f"unsafe P4E runtime database: {db_name}")
    return runtime


def _connect(login: str, password_env: str, *, autocommit: bool = False):
    host, port, db_name = _assert_same_disposable_topology()
    return psycopg.connect(
        host=host,
        port=port,
        dbname=db_name,
        user=login,
        password=os.environ[password_env],
        autocommit=autocommit,
    )


def _snapshot(login: str, password_env: str, signature: str):
    with _connect(login, password_env) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {signature}")
            columns = [item.name for item in cur.description]
            row = cur.fetchone()
        conn.commit()
    return columns, row


def _insert_search_runtime_rows() -> None:
    org_id = uuid.uuid4()
    branch_with_work = uuid.uuid4()
    branch_reconciliation_only = uuid.uuid4()
    pending_id = uuid.uuid4()
    processing_id = uuid.uuid4()
    dead_letter_id = uuid.uuid4()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                ) VALUES (%s,%s,%s,'basic',true,10,'INR')
                """,
                (
                    org_id,
                    f"P4E Runtime {org_id}",
                    f"p4e-runtime-{org_id.hex}",
                ),
            )
            cur.execute(
                "SELECT pg_catalog.set_config('app.current_org_id', %s, true)",
                (str(org_id),),
            )
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,
                    country_code,currency_code
                ) VALUES
                    (%s,%s,'P4E Search Work','P4E-W',%s,'IN','INR'),
                    (%s,%s,'P4E Search Reconcile','P4E-R',%s,'IN','INR')
                """,
                (
                    branch_with_work,
                    org_id,
                    f"p4e-work-{branch_with_work.hex}",
                    branch_reconciliation_only,
                    org_id,
                    f"p4e-reconcile-{branch_reconciliation_only.hex}",
                ),
            )
        conn.commit()

    # Use the already-certified application enqueue path. P4B's security owner
    # authority changes two rows to the other certified durable work states.
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_catalog.set_config('app.current_role','owner',true)"
            )
            cur.execute(
                "SELECT pg_catalog.set_config('app.current_org_id', %s, true)",
                (str(org_id),),
            )
            cur.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,tenant_id,branch_id,event_type,payload,
                    correlation_id
                ) VALUES
                    (%s,%s,%s,'branch.search_index','{}'::jsonb,%s),
                    (%s,%s,%s,'branch.search_deindex','{}'::jsonb,%s),
                    (%s,%s,%s,'branch.search_index','{}'::jsonb,%s)
                """,
                (
                    pending_id,
                    org_id,
                    branch_with_work,
                    uuid.uuid4(),
                    processing_id,
                    org_id,
                    branch_with_work,
                    uuid.uuid4(),
                    dead_letter_id,
                    org_id,
                    branch_with_work,
                    uuid.uuid4(),
                ),
            )
        conn.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute(
                """
                UPDATE public.branch_outbox_events
                SET status='processing',
                    leased_by=%s,
                    leased_until=pg_catalog.clock_timestamp()+interval '10 minutes'
                WHERE outbox_id=%s
                """,
                (uuid.uuid4(), processing_id),
            )
            cur.execute(
                """
                UPDATE public.branch_outbox_events
                SET status='dead_lettered',leased_by=NULL,leased_until=NULL
                WHERE outbox_id=%s
                """,
                (dead_letter_id,),
            )
            cur.execute("RESET ROLE")
        conn.commit()


def _insert_refund_runtime_rows() -> uuid.UUID:
    org_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    payment_id = uuid.uuid4()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,
                    default_currency_code
                ) VALUES (%s,%s,%s,'basic',true,10,'INR')
                """,
                (
                    org_id,
                    f"P4E Refund Runtime {org_id}",
                    f"p4e-refund-runtime-{org_id.hex}",
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(
                    id,code,legal_name,status
                ) VALUES (%s,%s,%s,'active')
                """,
                (
                    entity_id,
                    f"P4E_{entity_id.hex[:12].upper()}",
                    "P4E Runtime Legal Entity",
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES (
                    %s,%s,%s,'runtime_provider',%s,
                    1000000,'INR','captured'
                )
                """,
                (
                    payment_id,
                    org_id,
                    entity_id,
                    f"p4e-payment-{payment_id}",
                ),
            )
            cur.execute(
                """
                INSERT INTO finance.refunds(
                    id,organization_id,payment_id,legal_entity_id,
                    amount,currency_code,status,reason_code
                )
                SELECT
                    pg_catalog.gen_random_uuid(),%s,%s,%s,
                    1,'INR','approved',NULL
                FROM pg_catalog.generate_series(1,509)
                """,
                (org_id, payment_id, entity_id),
            )

            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute(
                """
                WITH ordered_refunds AS (
                    SELECT
                        r.id,
                        row_number() OVER (ORDER BY r.id) AS seq
                    FROM finance.refunds AS r
                    WHERE r.payment_id=%s
                )
                INSERT INTO finance.refund_execution_commands(
                    command_id,refund_id,payment_id,organization_id,
                    legal_entity_id,source_type,source_id,
                    logical_obligation_key,amount,currency_code,status,
                    attempt_count,max_attempts,lease_fence,process_after,
                    leased_by,leased_until,materialized_at,updated_at
                )
                SELECT
                    pg_catalog.gen_random_uuid(),r.id,%s,%s,%s,
                    'p4e_runtime',pg_catalog.gen_random_uuid(),
                    'p4e-runtime/' || r.id::text,1,'INR',
                    CASE
                        WHEN r.seq <= 501 THEN 'pending'
                        WHEN r.seq = 502 THEN 'processing'
                        WHEN r.seq = 503 THEN 'retry_pending'
                        WHEN r.seq = 504 THEN 'provider_accepted'
                        WHEN r.seq = 505 THEN 'reconciliation_pending'
                        WHEN r.seq = 506 THEN 'dead_lettered'
                        WHEN r.seq = 507 THEN 'succeeded'
                        WHEN r.seq = 508 THEN 'rejected'
                        ELSE 'cancelled'
                    END,
                    CASE WHEN r.seq=502 THEN 1 ELSE 0 END,
                    10,0,
                    pg_catalog.clock_timestamp()-interval '1 hour',
                    CASE WHEN r.seq=502 THEN pg_catalog.gen_random_uuid() ELSE NULL END,
                    CASE
                        WHEN r.seq=502
                        THEN pg_catalog.clock_timestamp()+interval '10 minutes'
                        ELSE NULL
                    END,
                    pg_catalog.clock_timestamp()-interval '2 hours',
                    pg_catalog.clock_timestamp()-interval '1 hour'
                FROM ordered_refunds AS r
                """,
                (payment_id, payment_id, org_id, entity_id),
            )

            # Leave a deliberately stale pending command behind a now-cancelled
            # parent refund. Certified P4D claim/discovery authority excludes it,
            # and P4E telemetry must exclude it too.
            cur.execute(
                """
                WITH ordered_refunds AS (
                    SELECT
                        r.id,
                        row_number() OVER (ORDER BY r.id) AS seq
                    FROM finance.refunds AS r
                    WHERE r.payment_id=%s
                )
                UPDATE finance.refunds AS r
                SET status='cancelled',updated_at=pg_catalog.clock_timestamp()
                FROM ordered_refunds AS ordered
                WHERE ordered.seq=501 AND r.id=ordered.id
                """,
                (payment_id,),
            )
            cur.execute("RESET ROLE")
        conn.commit()

    return payment_id


def test_runtime_database_is_explicit_and_disposable() -> None:
    host, port, db_name = _assert_same_disposable_topology()
    assert host in {"127.0.0.1", "localhost"}
    assert port > 0
    assert "test" in db_name


def test_snapshot_functions_are_maintenance_only_and_pii_free() -> None:
    expected = {
        _SEARCH_SNAPSHOT: _SEARCH_COLUMNS,
        _REFUND_SNAPSHOT: _REFUND_COLUMNS,
    }

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            for signature in expected:
                cur.execute(
                    """
                    SELECT owner.rolname,p.prosecdef,p.provolatile,p.proconfig
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_roles AS owner ON owner.oid=p.proowner
                    WHERE p.oid=pg_catalog.to_regprocedure(%s)
                    """,
                    (signature,),
                )
                owner, security_definer, volatility, config = cur.fetchone()
                assert owner == "app_security_owner"
                assert security_definer is True
                assert volatility == "s"
                assert "row_security=on" in set(config or ())
                assert any(
                    value.startswith("search_path=")
                    for value in (config or ())
                )

                for role, allowed in (
                    ("lifecycle_maintenance_runtime", True),
                    ("app_runtime", False),
                    ("auth_runtime", False),
                    ("worker_runtime", False),
                    ("finance_config_runtime", False),
                ):
                    cur.execute(
                        "SELECT pg_catalog.has_function_privilege(%s,%s,'EXECUTE')",
                        (role, signature),
                    )
                    assert cur.fetchone()[0] is allowed

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_proc AS p
                        CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                                p.proacl,
                                pg_catalog.acldefault('f', p.proowner)
                            )
                        ) AS acl
                        WHERE p.oid=pg_catalog.to_regprocedure(%s)
                          AND acl.grantee=0
                          AND acl.privilege_type='EXECUTE'
                    )
                    """,
                    (signature,),
                )
                assert cur.fetchone()[0] is False
        conn.commit()

    for signature, columns in expected.items():
        actual_columns, row = _snapshot(
            _MAINTENANCE_LOGIN,
            "MAINTENANCE_RUNTIME_PASSWORD",
            signature,
        )
        assert actual_columns == columns
        assert row is not None
        assert all(isinstance(value, (int, float)) for value in row)
        assert all(float(value) >= 0 for value in row)

    for login, password_env in (
        (_APP_LOGIN, "APP_RUNTIME_PASSWORD"),
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
    ):
        with _connect(login, password_env) as conn:
            for signature in expected:
                with pytest.raises(InsufficientPrivilege):
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT * FROM {signature}")
                conn.rollback()


def test_search_snapshot_tracks_durable_work_and_reconciliation_candidates() -> None:
    _, before = _snapshot(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        _SEARCH_SNAPSHOT,
    )
    _insert_search_runtime_rows()
    columns, after = _snapshot(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        _SEARCH_SNAPSHOT,
    )

    assert columns == _SEARCH_COLUMNS
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1
    assert after[2] == before[2] + 1
    assert after[3] >= before[3] + 1
    assert after[4] >= 0.0


def test_refund_snapshot_is_exact_beyond_500_and_matches_parent_eligibility() -> None:
    _, before = _snapshot(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        _REFUND_SNAPSHOT,
    )
    payment_id = _insert_refund_runtime_rows()
    columns, after = _snapshot(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        _REFUND_SNAPSHOT,
    )

    assert columns == _REFUND_COLUMNS
    # 501 commands were created pending, but one parent refund was then
    # cancelled. P4E must match P4D and report only 500 eligible pending rows.
    assert after[0] == before[0] + 500
    assert after[1] == before[1] + 1
    assert after[2] == before[2] + 1
    assert after[3] == before[3] + 1
    assert after[4] == before[4] + 1
    assert after[5] == before[5] + 1
    assert after[6] >= 60 * 60

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute(
                """
                SELECT count(*)
                FROM finance.refund_execution_commands AS c
                JOIN finance.refunds AS r ON r.id=c.refund_id
                WHERE c.payment_id=%s
                  AND c.status='pending'
                  AND r.status='cancelled'
                """,
                (payment_id,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute("RESET ROLE")
        conn.commit()

    with _connect(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM "
                "app_secure.discover_refund_execution_maintenance(500)"
            )
            assert cur.fetchone()[0] == 500
        conn.commit()

    unresolved_delta = sum(after[index] - before[index] for index in range(6))
    assert unresolved_delta == 505
