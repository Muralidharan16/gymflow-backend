from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


_DB_HOST = "127.0.0.1"
_DB_NAME = "gymflow_test"
_WORKER_LOGIN = "worker_test_runtime"
_MAINTENANCE_LOGIN = "lifecycle_maintenance_test_runtime"

# These are the exact relations scanned globally by the legacy P3E job families
# under review. P3E must not make those jobs functional by leaking cross-tenant
# table authority to an ordinary worker or maintenance login.
_LEGACY_GLOBAL_RELATIONS = (
    "public.member_subscriptions",
    "public.trial_subscriptions",
    "public.members",
    "public.gyms",
)
_DIRECT_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
_PLATFORM_MAINTENANCE_FUNCTIONS = (
    "app_secure.reclaim_stale_idempotency_keys(integer,integer)",
    "app_secure.archive_expired_idempotency_keys(integer,integer)",
    "app_secure.claim_due_geocoding_reverification(integer)",
    "app_secure.cleanup_expired_places_cache(integer)",
)


def _connect(login: str, password_env: str) -> psycopg.Connection:
    password = os.environ[password_env]
    return psycopg.connect(
        host=_DB_HOST,
        dbname=_DB_NAME,
        user=login,
        password=password,
        autocommit=True,
    )


def _assert_live_login(conn: psycopg.Connection, expected: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT session_user::text, current_user::text")
        assert cur.fetchone() == (expected, expected)


def _has_table_privilege(
    conn: psycopg.Connection,
    relation: str,
    privilege: str,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.has_table_privilege(current_user, %s, %s)",
            (relation, privilege),
        )
        return bool(cur.fetchone()[0])


def _has_function_execute(conn: psycopg.Connection, signature: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.has_function_privilege(current_user, %s, 'EXECUTE')",
            (signature,),
        )
        return bool(cur.fetchone()[0])


def _assert_no_direct_legacy_table_authority(conn: psycopg.Connection) -> None:
    leaked: list[str] = []
    for relation in _LEGACY_GLOBAL_RELATIONS:
        for privilege in _DIRECT_TABLE_PRIVILEGES:
            if _has_table_privilege(conn, relation, privilege):
                leaked.append(f"{privilege} {relation}")
    assert leaked == [], (
        "P3E legacy global-scan relations leaked direct background authority: "
        f"{leaked}"
    )


def test_worker_login_cannot_globally_read_or_mutate_legacy_job_relations() -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        _assert_live_login(conn, _WORKER_LOGIN)
        _assert_no_direct_legacy_table_authority(conn)

        # Live SQL must fail at PostgreSQL, rather than relying only on catalog
        # interpretation. An empty database is sufficient because permission is
        # checked before row visibility.
        for relation in _LEGACY_GLOBAL_RELATIONS:
            with pytest.raises(InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM {relation} LIMIT 1")


def test_maintenance_login_has_no_direct_legacy_job_table_authority() -> None:
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        _assert_live_login(conn, _MAINTENANCE_LOGIN)
        _assert_no_direct_legacy_table_authority(conn)


def test_worker_and_maintenance_logins_cannot_assume_each_other() -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        with pytest.raises(InsufficientPrivilege):
            with worker.cursor() as cur:
                cur.execute("SET ROLE lifecycle_maintenance_runtime")

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as maintenance:
        with pytest.raises(InsufficientPrivilege):
            with maintenance.cursor() as cur:
                cur.execute("SET ROLE worker_runtime")


def test_platform_maintenance_functions_are_not_worker_capabilities() -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        for signature in _PLATFORM_MAINTENANCE_FUNCTIONS:
            assert not _has_function_execute(conn, signature), (
                f"ordinary worker unexpectedly executes platform maintenance: {signature}"
            )


def test_platform_maintenance_capability_is_context_gated_and_bounded() -> None:
    signature = "app_secure.cleanup_expired_places_cache(integer)"
    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as conn:
        assert _has_function_execute(conn, signature)

        # Missing and wrong control-plane context must both fail closed.
        for context_value in (None, "lifecycle"):
            with conn.transaction():
                if context_value is not None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_catalog.set_config(" 
                            "'app.internal_maintenance', %s, true)",
                            (context_value,),
                        )
                with pytest.raises(InsufficientPrivilege):
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT app_secure.cleanup_expired_places_cache(1)"
                        )

        # Correct transaction-local context enables only the already-certified,
        # bounded SECURITY DEFINER capability. Fresh PG16 has no expired rows,
        # so the operation is expected to complete with a non-negative count.
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_catalog.set_config(" 
                    "'app.internal_maintenance', 'platform', true)"
                )
                cur.execute("SELECT app_secure.cleanup_expired_places_cache(1)")
                result = cur.fetchone()[0]
                assert isinstance(result, int)
                assert result >= 0

        # Transaction-local maintenance authority must not persist afterwards.
        with pytest.raises(InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT app_secure.cleanup_expired_places_cache(1)")
