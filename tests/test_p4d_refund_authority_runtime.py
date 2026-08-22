from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
    NoDataFound,
    SerializationFailure,
)


_DB_HOST = os.environ.get("P4D_REFUND_TEST_HOST", "127.0.0.1")
_DB_PORT = int(os.environ.get("PGPORT", "5432"))
_DB_NAME = os.environ.get("P4D_REFUND_TEST_DATABASE", "gymflow_p4d_test")
_ADMIN_LOGIN = "migration_owner"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_MAINTENANCE_LOGIN = "lifecycle_maintenance_test_runtime"

_ORG_A = uuid.UUID("d4100000-0000-4000-8000-000000000001")
_ORG_B = uuid.UUID("d4100000-0000-4000-8000-000000000002")
_ENTITY_A = uuid.UUID("d4100000-0000-4000-8000-000000000011")
_ENTITY_B = uuid.UUID("d4100000-0000-4000-8000-000000000012")
_PAYMENT_A = uuid.UUID("d4100000-0000-4000-8000-000000000021")
_PAYMENT_B = uuid.UUID("d4100000-0000-4000-8000-000000000022")
_REFUND_A = uuid.UUID("d4100000-0000-4000-8000-000000000031")
_BRANCH_A = uuid.UUID("d4100000-0000-4000-8000-000000000041")
_BRANCH_B = uuid.UUID("d4100000-0000-4000-8000-000000000042")
_SOURCE_A = uuid.UUID("d4100000-0000-4000-8000-000000000051")
_SOURCE_B = uuid.UUID("d4100000-0000-4000-8000-000000000052")
_SOURCE_WRONG_TYPE = uuid.UUID("d4100000-0000-4000-8000-000000000053")
_CORRELATION_A = uuid.UUID("d4100000-0000-4000-8000-000000000061")
_CORRELATION_B = uuid.UUID("d4100000-0000-4000-8000-000000000062")
_CORRELATION_WRONG_TYPE = uuid.UUID("d4100000-0000-4000-8000-000000000063")

_MATERIALIZE = "app_secure.materialize_refund_execution_command(uuid,text,uuid,text)"
_CLAIM = "app_secure.claim_refund_execution_command(uuid,integer)"
_FAILURE = "app_secure.record_refund_execution_failure(uuid,uuid,bigint,text,boolean)"
_DISCOVER = "app_secure.discover_refund_execution_maintenance(integer)"


def _validate_safe_p4d_database(db_name: str = _DB_NAME, host: str = _DB_HOST) -> None:
    if db_name != "gymflow_p4d_test":
        raise RuntimeError(f"unsafe P4D refund runtime database: {db_name}")
    if db_name in {"gymflow", "gymflow_test", "gymflow_migration_test", "production"}:
        raise RuntimeError(f"unsafe P4D refund runtime database: {db_name}")
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"unsafe P4D refund runtime host: {host}")


def _connect(login: str, password_env: str, *, autocommit: bool = False):
    return psycopg.connect(
        host=_DB_HOST,
        port=_DB_PORT,
        dbname=_DB_NAME,
        user=login,
        password=os.environ[password_env],
        autocommit=autocommit,
    )


def _reset_state() -> None:
    _validate_safe_p4d_database()
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE finance.refund_execution_commands")
            cur.execute(
                "DELETE FROM finance.refunds WHERE id = ANY(%s)",
                ([_REFUND_A],),
            )
            cur.execute(
                "DELETE FROM finance.payments WHERE id = ANY(%s)",
                ([_PAYMENT_A, _PAYMENT_B],),
            )
            cur.execute(
                "DELETE FROM finance.legal_entities WHERE id = ANY(%s)",
                ([_ENTITY_A, _ENTITY_B],),
            )
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code
                ) VALUES
                    (%s,'P4D Runtime Org A','p4d-runtime-org-a','basic',true,10,'INR'),
                    (%s,'P4D Runtime Org B','p4d-runtime-org-b','basic',true,10,'INR')
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name,
                    slug=EXCLUDED.slug,
                    tier=EXCLUDED.tier,
                    is_active=EXCLUDED.is_active,
                    max_branches=EXCLUDED.max_branches,
                    default_currency_code=EXCLUDED.default_currency_code
                """,
                (_ORG_A, _ORG_B),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(id,code,legal_name,status)
                VALUES
                    (%s,'P4D_RUNTIME_A','P4D Runtime Entity A','active'),
                    (%s,'P4D_RUNTIME_B','P4D Runtime Entity B','active')
                """,
                (_ENTITY_A, _ENTITY_B),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES
                    (%s,%s,%s,'runtime_provider','runtime_payment_a',100,'INR','captured'),
                    (%s,%s,%s,'runtime_provider','runtime_payment_b',100,'INR','captured')
                """,
                (
                    _PAYMENT_A,
                    _ORG_A,
                    _ENTITY_A,
                    _PAYMENT_B,
                    _ORG_B,
                    _ENTITY_B,
                ),
            )
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                ) VALUES (%s,%s,'P4D Runtime Branch A','P4D-A','p4d-runtime-a','IN','INR')
                ON CONFLICT (id) DO UPDATE SET
                    org_id=EXCLUDED.org_id,
                    branch_name=EXCLUDED.branch_name,
                    branch_code=EXCLUDED.branch_code,
                    internal_slug=EXCLUDED.internal_slug,
                    country_code=EXCLUDED.country_code,
                    currency_code=EXCLUDED.currency_code
                """,
                (_BRANCH_A, _ORG_A),
            )
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_B),))
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                ) VALUES (%s,%s,'P4D Runtime Branch B','P4D-B','p4d-runtime-b','IN','INR')
                ON CONFLICT (id) DO UPDATE SET
                    org_id=EXCLUDED.org_id,
                    branch_name=EXCLUDED.branch_name,
                    branch_code=EXCLUDED.branch_code,
                    internal_slug=EXCLUDED.internal_slug,
                    country_code=EXCLUDED.country_code,
                    currency_code=EXCLUDED.currency_code
                """,
                (_BRANCH_B, _ORG_B),
            )
            cur.execute(
                """
                INSERT INTO finance.refunds(
                    id,organization_id,payment_id,legal_entity_id,
                    amount,currency_code,status,reason_code
                ) VALUES (%s,%s,%s,%s,25,'INR','approved','p4d-runtime')
                """,
                (_REFUND_A, _ORG_A, _PAYMENT_A, _ENTITY_A),
            )
        conn.commit()

    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_role', 'owner', true)")
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,branch_id,tenant_id,event_type,payload,correlation_id
                ) VALUES
                    (%s,%s,%s,'branch.refund_required','{}'::jsonb,%s),
                    (%s,%s,%s,'branch.search_index','{}'::jsonb,%s)
                ON CONFLICT (outbox_id) DO NOTHING
                """,
                (_SOURCE_A, _BRANCH_A, _ORG_A, _CORRELATION_A, _SOURCE_WRONG_TYPE, _BRANCH_A, _ORG_A, _CORRELATION_WRONG_TYPE),
            )
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_B),))
            cur.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,branch_id,tenant_id,event_type,payload,correlation_id
                ) VALUES (%s,%s,%s,'branch.refund_required','{}'::jsonb,%s)
                ON CONFLICT (outbox_id) DO NOTHING
                """,
                (_SOURCE_B, _BRANCH_B, _ORG_B, _CORRELATION_B),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_refund_authority_state() -> None:
    _reset_state()


def test_destructive_runtime_database_validator_rejects_non_p4d_databases() -> None:
    _validate_safe_p4d_database("gymflow_p4d_test", "127.0.0.1")
    for unsafe in ("gymflow_test", "gymflow_migration_test", "gymflow", "production", "arbitrary_env_db"):
        with pytest.raises(RuntimeError):
            _validate_safe_p4d_database(unsafe, "127.0.0.1")
    with pytest.raises(RuntimeError):
        _validate_safe_p4d_database("gymflow_p4d_test", "db.prod.internal")


def _materialize(idempotency_key: str):
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                (
                    _REFUND_A,
                    "branch.refund_required",
                    _SOURCE_A,
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def _security_owner_fetchone(sql: str, params: tuple[object, ...] = ()):
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return row


def _security_owner_execute(sql: str, params: tuple[object, ...] = ()) -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute(sql, params)
        conn.commit()


def test_refund_capability_acl_matrix_and_no_direct_runtime_table_dml() -> None:
    expected = {
        _MATERIALIZE: {_WORKER_LOGIN},
        _CLAIM: {_WORKER_LOGIN},
        _FAILURE: {_WORKER_LOGIN},
        _DISCOVER: {_MAINTENANCE_LOGIN},
    }
    logins = (
        (_APP_LOGIN, "APP_RUNTIME_PASSWORD"),
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
        (_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD"),
    )
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 'finance.refund_execution_commands'::regclass::oid"
            )
            command_relation_oid = cur.fetchone()[0]

    for login, password_env in logins:
        with _connect(login, password_env, autocommit=True) as conn:
            with conn.cursor() as cur:
                for signature, allowed in expected.items():
                    cur.execute(
                        "SELECT pg_catalog.has_function_privilege(current_user,%s,'EXECUTE')",
                        (signature,),
                    )
                    assert cur.fetchone()[0] is (login in allowed)
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    cur.execute(
                        "SELECT pg_catalog.has_table_privilege("
                        "current_user,%s::oid,%s)",
                        (command_relation_oid, privilege),
                    )
                    assert cur.fetchone()[0] is False

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM finance.refund_execution_commands"
            )
            assert cur.fetchone()[0] == 0


def test_materialization_derives_authority_and_is_stable_across_transactions() -> None:
    first = _materialize("runtime-first")
    replay = _materialize("runtime-replay")

    assert first[0] == replay[0]
    assert first[1:6] == (
        _REFUND_A,
        _PAYMENT_A,
        _ORG_A,
        25,
        "INR",
    )
    assert first[6:] == (0, "pending", False)
    assert replay[6:] == (0, "pending", True)
    assert _security_owner_fetchone(
        "SELECT count(*) FROM finance.refund_execution_commands "
        "WHERE refund_id=%s AND logical_obligation_key=%s",
        (_REFUND_A, f"finance-refund/{_REFUND_A}"),
    )[0] == 1


def test_concurrent_materialization_creates_exactly_one_logical_command() -> None:
    barrier = threading.Barrier(2)

    def invoke(index: int):
        with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
            with conn.cursor() as cur:
                barrier.wait(timeout=5)
                cur.execute(
                    "SELECT command_id,reused FROM "
                    "app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (
                        _REFUND_A,
                        "branch.refund_required",
                        _SOURCE_A,
                        f"runtime-concurrent-{index}",
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return row

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, range(2)))

    assert results[0][0] == results[1][0]
    assert sorted(row[1] for row in results) == [False, True]
    assert _security_owner_fetchone(
        "SELECT count(*) FROM finance.refund_execution_commands WHERE refund_id=%s",
        (_REFUND_A,),
    )[0] == 1


def test_materialization_rejects_missing_invalid_and_cross_tenant_authority() -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(NoDataFound):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (
                        uuid.uuid4(),
                        "branch.refund_required",
                        _SOURCE_A,
                        "missing",
                    ),
                )
        conn.rollback()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE finance.refunds SET status='rejected' WHERE id=%s",
                (_REFUND_A,),
            )
        conn.commit()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (
                        _REFUND_A,
                        "branch.refund_required",
                        _SOURCE_A,
                        "invalid-status",
                    ),
                )
        conn.rollback()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE finance.refunds SET status='approved',organization_id=%s "
                "WHERE id=%s",
                (_ORG_B, _REFUND_A),
            )
        conn.commit()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (
                        _REFUND_A,
                        "branch.refund_required",
                        _SOURCE_A,
                        "tenant-mismatch",
                    ),
                )
        conn.rollback()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE finance.refunds SET organization_id=%s,payment_id=%s "
                "WHERE id=%s",
                (_ORG_A, _PAYMENT_B, _REFUND_A),
            )
        conn.commit()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (
                        _REFUND_A,
                        "branch.refund_required",
                        _SOURCE_A,
                        "payment-mismatch",
                    ),
                )
        conn.rollback()


def test_source_outbox_tenant_and_type_are_authoritative() -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (_REFUND_A, "branch.refund_required", _SOURCE_B, "source-org-b"),
                )
        conn.rollback()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(NoDataFound):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (_REFUND_A, "branch.refund_required", uuid.uuid4(), "source-missing"),
                )
        conn.rollback()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.materialize_refund_execution_command(%s,%s,%s,%s)",
                    (_REFUND_A, "branch.refund_required", _SOURCE_WRONG_TYPE, "source-wrong-type"),
                )
        conn.rollback()

    assert _materialize("source-valid")[7:] == ("pending", False)


def test_cancelled_refund_command_is_not_claimable_but_history_remains() -> None:
    command_id = _materialize("runtime-cancelled-command")[0]
    worker = uuid.UUID("d4100000-0000-4000-8000-000000000081")
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE finance.refunds SET status='cancelled' WHERE id=%s", (_REFUND_A,))
        conn.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT command_id FROM app_secure.claim_refund_execution_command(%s,1)", (worker,))
            assert cur.fetchone() is None
        conn.commit()

    assert _security_owner_fetchone(
        "SELECT count(*) FROM finance.refund_execution_commands WHERE command_id=%s",
        (command_id,),
    )[0] == 1

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT command_id FROM app_secure.discover_refund_execution_maintenance(10)")
            assert command_id not in {row[0] for row in cur.fetchall()}
        conn.commit()


def test_record_failure_rejects_unsafe_error_codes_and_maintenance_exposes_machine_code_only() -> None:
    command_id = _materialize("runtime-error-code")[0]
    worker = uuid.UUID("d4100000-0000-4000-8000-000000000091")
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lease_fence FROM app_secure.claim_refund_execution_command(%s,1)",
                (worker,),
            )
            fence = cur.fetchone()[0]
        conn.commit()

    for unsafe in (
        "This is an exception sentence",
        "line\nbreak",
        "https://example.test/token",
        "bearer_secret_token_value_that_should_not_be_persisted",
        "person@example.test",
        "a" * 65,
    ):
        with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
            with pytest.raises(CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                        (command_id, worker, fence, unsafe),
                    )
            conn.rollback()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                (command_id, worker, fence, "provider_timeout"),
            )
            assert cur.fetchone()[0] == "retry_pending"
        conn.commit()

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_error_code FROM app_secure.discover_refund_execution_maintenance(10)")
            assert cur.fetchone()[0] == "provider_timeout"
        conn.commit()


def test_migration_owner_cannot_read_nonempty_force_rls_command_table() -> None:
    command_id = _materialize("runtime-migration-owner-rls")[0]
    assert _security_owner_fetchone(
        "SELECT count(*) FROM finance.refund_execution_commands WHERE command_id=%s",
        (command_id,),
    )[0] == 1
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM finance.refund_execution_commands")
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class "
                "WHERE oid='finance.refund_execution_commands'::regclass"
            )
            assert cur.fetchone() == (True, True)
            cur.execute("SELECT rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user")
            assert cur.fetchone()[0] is False
            cur.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_policy p
                JOIN pg_catalog.pg_roles r ON r.oid = ANY(p.polroles)
                WHERE p.polrelid='finance.refund_execution_commands'::regclass
                  AND r.rolname='migration_owner'
                """
            )
            assert cur.fetchone()[0] == 0
        conn.commit()


def test_refund_payment_currency_cannot_diverge_under_direct_sql() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with pytest.raises(ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE finance.refunds SET currency_code='USD' WHERE id=%s",
                    (_REFUND_A,),
                )
        conn.rollback()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.proargnames
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='app_secure'
                  AND p.proname='materialize_refund_execution_command'
                """
            )
            assert cur.fetchone()[0][:4] == [
                "p_refund_id",
                "p_source_type",
                "p_source_id",
                "p_idempotency_key",
            ]


def test_worker_claim_fence_retry_and_sanitized_maintenance_discovery() -> None:
    command_id = _materialize("runtime-claim")[0]
    worker_one = uuid.UUID("d4100000-0000-4000-8000-000000000051")
    worker_two = uuid.UUID("d4100000-0000-4000-8000-000000000052")

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt FROM "
                "app_secure.claim_refund_execution_command(%s,1)",
                (worker_one,),
            )
            assert cur.fetchone() == (command_id, 1, 1, False)
        conn.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id FROM "
                "app_secure.claim_refund_execution_command(%s,1)",
                (worker_two,),
            )
            assert cur.fetchone() is None
        conn.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(SerializationFailure):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                    (command_id, worker_two, 1, "stale_worker"),
                )
        conn.rollback()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                (command_id, worker_one, 1, "transient"),
            )
            assert cur.fetchone()[0] == "retry_pending"
        conn.commit()

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.discover_refund_execution_maintenance(10)"
            )
            assert [item.name for item in cur.description] == [
                "command_id",
                "organization_id",
                "status",
                "attempt_count",
                "process_after",
                "leased_until",
                "last_error_code",
            ]
            row = cur.fetchone()
            assert row[0] == command_id
            assert row[1] == _ORG_A
            assert row[2] == "retry_pending"
        conn.commit()

    _security_owner_execute(
        "UPDATE finance.refund_execution_commands "
        "SET process_after=clock_timestamp()-interval '1 second' "
        "WHERE command_id=%s",
        (command_id,),
    )
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt FROM "
                "app_secure.claim_refund_execution_command(%s,1)",
                (worker_two,),
            )
            assert cur.fetchone() == (command_id, 2, 2, False)
        conn.commit()


def test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence() -> None:
    command_id = _materialize("runtime-expired-lease")[0]
    worker = uuid.UUID("d4100000-0000-4000-8000-000000000061")

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt "
                "FROM app_secure.claim_refund_execution_command(%s,1)",
                (worker,),
            )
            assert cur.fetchone() == (command_id, 1, 1, False)
        conn.commit()

    _security_owner_execute(
        "UPDATE finance.refund_execution_commands "
        "SET leased_until=clock_timestamp()-interval '1 second' "
        "WHERE command_id=%s",
        (command_id,),
    )
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt "
                "FROM app_secure.claim_refund_execution_command(%s,1)",
                (worker,),
            )
            assert cur.fetchone() == (command_id, 1, 2, True)
        conn.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(SerializationFailure):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                    (command_id, worker, 1, "expired_owner"),
                )
        conn.rollback()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                (command_id, worker, 2, "current_owner"),
            )
            assert cur.fetchone()[0] == "retry_pending"
        conn.commit()


def test_final_attempt_expired_processing_command_is_recoverable_without_incrementing_attempt() -> None:
    command_id = _materialize("runtime-final-attempt")[0]
    worker_one = uuid.UUID("d4100000-0000-4000-8000-000000000071")
    worker_two = uuid.UUID("d4100000-0000-4000-8000-000000000072")
    _security_owner_execute(
        "UPDATE finance.refund_execution_commands SET max_attempts=1 WHERE command_id=%s",
        (command_id,),
    )

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt "
                "FROM app_secure.claim_refund_execution_command(%s,1)",
                (worker_one,),
            )
            assert cur.fetchone() == (command_id, 1, 1, False)
        conn.commit()

    _security_owner_execute(
        "UPDATE finance.refund_execution_commands "
        "SET leased_until=clock_timestamp()-interval '1 second' "
        "WHERE command_id=%s",
        (command_id,),
    )
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command_id,attempt_count,lease_fence,reclaimed_existing_attempt "
                "FROM app_secure.claim_refund_execution_command(%s,1)",
                (worker_two,),
            )
            assert cur.fetchone() == (command_id, 1, 2, True)
        conn.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(SerializationFailure):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT app_secure.record_refund_execution_failure(%s,%s,%s,%s,false)",
                    (command_id, worker_one, 1, "final_stale"),
                )
        conn.rollback()

    assert _security_owner_fetchone(
        "SELECT status,attempt_count,max_attempts,lease_fence FROM finance.refund_execution_commands WHERE command_id=%s",
        (command_id,),
    ) == ("processing", 1, 1, 2)


def test_app_and_maintenance_cannot_claim_and_app_cannot_discover() -> None:
    for login, password_env, signature, args in (
        (
            _APP_LOGIN,
            "APP_RUNTIME_PASSWORD",
            "claim_refund_execution_command",
            (uuid.uuid4(), 1),
        ),
        (
            _MAINTENANCE_LOGIN,
            "MAINTENANCE_RUNTIME_PASSWORD",
            "claim_refund_execution_command",
            (uuid.uuid4(), 1),
        ),
        (
            _APP_LOGIN,
            "APP_RUNTIME_PASSWORD",
            "discover_refund_execution_maintenance",
            (10,),
        ),
    ):
        placeholders = ",".join(["%s"] * len(args))
        with _connect(login, password_env) as conn:
            with pytest.raises(InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT * FROM app_secure.{signature}({placeholders})",
                        args,
                    )
            conn.rollback()
