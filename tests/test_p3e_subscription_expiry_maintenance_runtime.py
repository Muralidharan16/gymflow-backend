from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


_DB_HOST = "127.0.0.1"
_DB_NAME = "gymflow_test"
_ADMIN_LOGIN = "migration_owner"
_APP_LOGIN = "app_test_runtime"
_AUTH_LOGIN = "auth_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"
_MAINTENANCE_LOGIN = "lifecycle_maintenance_test_runtime"
_SIGNATURE = "app_secure.expire_legacy_member_subscriptions(integer)"

_ORG_ID = uuid.UUID("e3000000-0000-4000-8000-000000000001")
_GYM_ID = uuid.UUID("e3000000-0000-4000-8000-000000000002")
_MEMBER_ID = uuid.UUID("e3000000-0000-4000-8000-000000000003")
_PLAN_ID = uuid.UUID("e3000000-0000-4000-8000-000000000004")
_SUB_IDS = {
    "due_a": uuid.UUID("e3000000-0000-4000-8000-000000000011"),
    "due_b": uuid.UUID("e3000000-0000-4000-8000-000000000012"),
    "future": uuid.UUID("e3000000-0000-4000-8000-000000000013"),
    "already_expired": uuid.UUID("e3000000-0000-4000-8000-000000000014"),
}


def _connect(login: str, password_env: str, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        host=_DB_HOST,
        dbname=_DB_NAME,
        user=login,
        password=os.environ[password_env],
        autocommit=autocommit,
    )


def _set_platform_context(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.set_config("
            "'app.internal_maintenance', 'platform', true)"
        )


def _call_capability(conn: psycopg.Connection, batch_size: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT app_secure.expire_legacy_member_subscriptions(%s)",
            (batch_size,),
        )
        return int(cur.fetchone()[0])


def _reset_subscription_rows() -> None:
    today = date.today()
    rows = (
        (_SUB_IDS["due_a"], today - timedelta(days=10), "active"),
        (_SUB_IDS["due_b"], today - timedelta(days=5), "active"),
        (_SUB_IDS["future"], today + timedelta(days=5), "active"),
        (_SUB_IDS["already_expired"], today - timedelta(days=20), "expired"),
    )
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.member_subscriptions WHERE id = ANY(%s::uuid[])",
                ([str(value) for value in _SUB_IDS.values()],),
            )
            for subscription_id, end_date, status in rows:
                cur.execute(
                    """
                    INSERT INTO public.member_subscriptions (
                        id, gym_id, member_id, plan_id, start_date, end_date,
                        total_freeze_days, status, reminder_sent
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, 0,
                        %s::public.subscriptionstatus, false
                    )
                    """,
                    (
                        subscription_id,
                        _GYM_ID,
                        _MEMBER_ID,
                        _PLAN_ID,
                        end_date - timedelta(days=30),
                        end_date,
                        status,
                    ),
                )


def _statuses() -> dict[uuid.UUID, str]:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status::text
                FROM public.member_subscriptions
                WHERE id = ANY(%s::uuid[])
                ORDER BY id
                """,
                ([str(value) for value in _SUB_IDS.values()],),
            )
            return {row[0]: row[1] for row in cur.fetchall()}


@pytest.fixture(autouse=True)
def _fresh_subscription_state() -> None:
    _reset_subscription_rows()


def test_only_maintenance_can_execute_expiry_capability() -> None:
    for login, password_env in (
        (_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD"),
        (_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD"),
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
    ):
        with _connect(login, password_env) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_catalog.has_function_privilege("
                    "current_user, %s, 'EXECUTE')",
                    (_SIGNATURE,),
                )
                assert cur.fetchone()[0] is False

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_catalog.has_function_privilege("
                "current_user, %s, 'EXECUTE')",
                (_SIGNATURE,),
            )
            assert cur.fetchone()[0] is True


def test_background_logins_keep_zero_direct_subscription_table_authority() -> None:
    for login, password_env in (
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
        (_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD"),
    ):
        with _connect(login, password_env) as conn:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_catalog.has_table_privilege("
                        "current_user, 'public.member_subscriptions', %s)",
                        (privilege,),
                    )
                    assert cur.fetchone()[0] is False


def test_missing_wrong_and_invalid_commands_fail_closed() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with pytest.raises(InsufficientPrivilege):
            _call_capability(conn, 1)

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_catalog.set_config("
                    "'app.internal_maintenance', 'lifecycle', true)"
                )
            with pytest.raises(InsufficientPrivilege):
                _call_capability(conn, 1)

        for invalid_batch in (0, -1, 1001):
            with conn.transaction():
                _set_platform_context(conn)
                with pytest.raises(InsufficientPrivilege):
                    _call_capability(conn, invalid_batch)


def test_capability_changes_only_due_active_rows_and_honors_batch_bound() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_platform_context(conn)
            assert _call_capability(conn, 1) == 1

        after_first = _statuses()
        assert after_first[_SUB_IDS["due_a"]] == "expired"
        assert after_first[_SUB_IDS["due_b"]] == "active"
        assert after_first[_SUB_IDS["future"]] == "active"
        assert after_first[_SUB_IDS["already_expired"]] == "expired"

        with conn.transaction():
            _set_platform_context(conn)
            assert _call_capability(conn, 1) == 1

        with conn.transaction():
            _set_platform_context(conn)
            assert _call_capability(conn, 1000) == 0

    final = _statuses()
    assert final[_SUB_IDS["due_a"]] == "expired"
    assert final[_SUB_IDS["due_b"]] == "expired"
    assert final[_SUB_IDS["future"]] == "active"
    assert final[_SUB_IDS["already_expired"]] == "expired"


def test_transaction_rollback_does_not_partially_persist_expiry() -> None:
    conn = _connect(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        autocommit=False,
    )
    try:
        _set_platform_context(conn)
        assert _call_capability(conn, 1) == 1
        conn.rollback()
    finally:
        conn.close()

    statuses = _statuses()
    assert statuses[_SUB_IDS["due_a"]] == "active"
    assert statuses[_SUB_IDS["due_b"]] == "active"


def test_competing_maintenance_calls_process_distinct_rows() -> None:
    barrier = threading.Barrier(2)

    def invoke() -> int:
        conn = _connect(
            _MAINTENANCE_LOGIN,
            "MAINTENANCE_RUNTIME_PASSWORD",
            autocommit=False,
        )
        try:
            _set_platform_context(conn)
            barrier.wait(timeout=5)
            count = _call_capability(conn, 1)
            conn.commit()
            return count
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        counts = list(executor.map(lambda _: invoke(), range(2)))

    assert sorted(counts) == [1, 1]
    statuses = _statuses()
    assert statuses[_SUB_IDS["due_a"]] == "expired"
    assert statuses[_SUB_IDS["due_b"]] == "expired"
    assert statuses[_SUB_IDS["future"]] == "active"


def test_maintenance_context_is_transaction_local() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_platform_context(conn)
            assert _call_capability(conn, 1) == 1

        with pytest.raises(InsufficientPrivilege):
            _call_capability(conn, 1)
