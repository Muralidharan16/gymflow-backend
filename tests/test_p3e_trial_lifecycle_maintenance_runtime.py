from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
_SIGNATURE = "app_secure.advance_trial_lifecycles(integer)"

_ORG_IDS = {
    "soft_due": uuid.UUID("e3100000-0000-4000-8000-000000000001"),
    "hard_due": uuid.UUID("e3100000-0000-4000-8000-000000000002"),
    "future": uuid.UUID("e3100000-0000-4000-8000-000000000003"),
    "converted": uuid.UUID("e3100000-0000-4000-8000-000000000004"),
}
_TRIAL_IDS = {
    key: uuid.UUID(f"e3100000-0000-4000-8000-{index:012d}")
    for index, key in enumerate(_ORG_IDS, start=11)
}
_AUDIT_ACTIONS = ("TRIAL_SOFT_LOCKED", "TRIAL_HARD_LOCKED")


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


def _call_capability(
    conn: psycopg.Connection,
    batch_size: int,
) -> list[tuple[uuid.UUID, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT organization_id, new_status
            FROM app_secure.advance_trial_lifecycles(%s)
            ORDER BY organization_id
            """,
            (batch_size,),
        )
        return [(row[0], str(row[1])) for row in cur.fetchall()]


def _reset_trials() -> None:
    now = datetime.now(timezone.utc)
    rows = (
        (
            "soft_due",
            "active",
            now - timedelta(days=7),
            now - timedelta(hours=2),
            now + timedelta(days=1),
            now + timedelta(days=2),
        ),
        (
            "hard_due",
            "soft_locked",
            now - timedelta(days=7),
            now - timedelta(days=3),
            now - timedelta(days=1),
            now - timedelta(hours=1),
        ),
        (
            "future",
            "active",
            now,
            now + timedelta(days=1),
            now + timedelta(days=2),
            now + timedelta(days=3),
        ),
        (
            "converted",
            "converted",
            now - timedelta(days=10),
            now - timedelta(days=5),
            now - timedelta(days=4),
            now - timedelta(days=3),
        ),
    )

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.audit_logs "
                "WHERE organization_id = ANY(%s::uuid[]) "
                "AND action = ANY(%s::text[])",
                (
                    [str(value) for value in _ORG_IDS.values()],
                    list(_AUDIT_ACTIONS),
                ),
            )
            cur.execute(
                "DELETE FROM public.trial_subscriptions "
                "WHERE id = ANY(%s::uuid[])",
                ([str(value) for value in _TRIAL_IDS.values()],),
            )
            for key, status, trial_start, trial_end, grace_end, hard_lock_at in rows:
                cur.execute(
                    """
                    INSERT INTO public.trial_subscriptions (
                        id, organization_id, trial_start, trial_end,
                        grace_end, hard_lock_at, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        _TRIAL_IDS[key],
                        _ORG_IDS[key],
                        trial_start,
                        trial_end,
                        grace_end,
                        hard_lock_at,
                        status,
                    ),
                )


def _trial_statuses() -> dict[uuid.UUID, str]:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT organization_id, status
                FROM public.trial_subscriptions
                WHERE organization_id = ANY(%s::uuid[])
                ORDER BY organization_id
                """,
                ([str(value) for value in _ORG_IDS.values()],),
            )
            return {row[0]: str(row[1]) for row in cur.fetchall()}


def _audits() -> list[tuple[uuid.UUID, str, dict]]:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT organization_id, action, metadata_json
                FROM public.audit_logs
                WHERE organization_id = ANY(%s::uuid[])
                  AND action = ANY(%s::text[])
                ORDER BY organization_id, action
                """,
                (
                    [str(value) for value in _ORG_IDS.values()],
                    list(_AUDIT_ACTIONS),
                ),
            )
            return [(row[0], str(row[1]), row[2]) for row in cur.fetchall()]


@pytest.fixture(autouse=True)
def _fresh_trial_state() -> None:
    _reset_trials()


def test_only_maintenance_can_execute_trial_lifecycle_capability() -> None:
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


def test_maintenance_has_no_direct_trial_or_audit_table_authority() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        for relation in ("public.trial_subscriptions", "public.audit_logs"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_catalog.has_table_privilege("
                        "current_user, %s, %s)",
                        (relation, privilege),
                    )
                    assert cur.fetchone()[0] is False


def test_missing_wrong_and_invalid_trial_commands_fail_closed() -> None:
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

        for invalid_batch in (0, -1, 501):
            with conn.transaction():
                _set_platform_context(conn)
                with pytest.raises(InsufficientPrivilege):
                    _call_capability(conn, invalid_batch)


def test_due_trial_transitions_and_audits_are_atomic_and_bounded() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_platform_context(conn)
            first = _call_capability(conn, 1)
        assert len(first) == 1

        with conn.transaction():
            _set_platform_context(conn)
            second = _call_capability(conn, 1)
        assert len(second) == 1

        with conn.transaction():
            _set_platform_context(conn)
            assert _call_capability(conn, 500) == []

    statuses = _trial_statuses()
    assert statuses[_ORG_IDS["soft_due"]] == "soft_locked"
    assert statuses[_ORG_IDS["hard_due"]] == "hard_locked"
    assert statuses[_ORG_IDS["future"]] == "active"
    assert statuses[_ORG_IDS["converted"]] == "converted"

    audits = _audits()
    assert len(audits) == 2
    by_org = {row[0]: row for row in audits}
    assert by_org[_ORG_IDS["soft_due"]][1] == "TRIAL_SOFT_LOCKED"
    assert by_org[_ORG_IDS["hard_due"]][1] == "TRIAL_HARD_LOCKED"
    for _, _, metadata in audits:
        assert metadata["source"] == "p3e_maintenance"
        assert metadata["previous_status"] in {"active", "soft_locked"}
        assert metadata["new_status"] in {"soft_locked", "hard_locked"}


def test_trial_transition_and_audit_rollback_together() -> None:
    conn = _connect(
        _MAINTENANCE_LOGIN,
        "MAINTENANCE_RUNTIME_PASSWORD",
        autocommit=False,
    )
    try:
        _set_platform_context(conn)
        assert len(_call_capability(conn, 1)) == 1
        conn.rollback()
    finally:
        conn.close()

    statuses = _trial_statuses()
    assert statuses[_ORG_IDS["soft_due"]] == "active"
    assert statuses[_ORG_IDS["hard_due"]] == "soft_locked"
    assert _audits() == []


def test_competing_trial_maintenance_calls_do_not_process_same_row() -> None:
    barrier = threading.Barrier(2)

    def invoke() -> list[tuple[uuid.UUID, str]]:
        conn = _connect(
            _MAINTENANCE_LOGIN,
            "MAINTENANCE_RUNTIME_PASSWORD",
            autocommit=False,
        )
        try:
            _set_platform_context(conn)
            barrier.wait(timeout=5)
            rows = _call_capability(conn, 1)
            conn.commit()
            return rows
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))

    assert [len(rows) for rows in results] == [1, 1]
    processed = [row[0] for rows in results for row in rows]
    assert len(set(processed)) == 2

    statuses = _trial_statuses()
    assert statuses[_ORG_IDS["soft_due"]] == "soft_locked"
    assert statuses[_ORG_IDS["hard_due"]] == "hard_locked"
    assert len(_audits()) == 2


def test_trial_maintenance_context_does_not_leak_after_commit() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_platform_context(conn)
            assert len(_call_capability(conn, 1)) == 1

        with pytest.raises(InsufficientPrivilege):
            _call_capability(conn, 1)
