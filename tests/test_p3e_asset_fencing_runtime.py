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

_ORG1 = uuid.UUID("e3200000-0000-4000-8000-000000000001")
_ORG2 = uuid.UUID("e3200000-0000-4000-8000-000000000002")
_OWNER1 = uuid.UUID("e3200000-0000-4000-8000-000000000011")
_OWNER2 = uuid.UUID("e3200000-0000-4000-8000-000000000012")

_ENQUEUE = "app_secure.enqueue_organization_asset_job(text,text,numeric,text)"
_CLAIM = "app_secure.claim_organization_asset_job(uuid,uuid,integer)"
_FINALIZE = "app_secure.finalize_organization_asset_job(uuid,uuid,integer,integer,bigint,text)"
_FAIL = "app_secure.fail_organization_asset_job(uuid,uuid,text)"
_DELETE = "app_secure.delete_current_organization_asset(text)"
_ASSET_DISPATCH = "app_secure.dispatchable_organization_asset_jobs(integer)"
_CLEANUP_CLAIM = "app_secure.claim_organization_asset_cleanup(uuid,uuid,integer)"
_CLEANUP_COMPLETE = "app_secure.complete_organization_asset_cleanup(uuid,uuid)"
_CLEANUP_FAIL = "app_secure.fail_organization_asset_cleanup(uuid,uuid,text)"
_CLEANUP_DISPATCH = "app_secure.dispatchable_organization_asset_cleanup(integer)"


def _connect(login: str, password_env: str, *, autocommit: bool = False):
    return psycopg.connect(
        host=_DB_HOST,
        dbname=_DB_NAME,
        user=login,
        password=os.environ[password_env],
        autocommit=autocommit,
    )


def _set_app_context(
    conn,
    *,
    org_id: uuid.UUID = _ORG1,
    user_id: uuid.UUID = _OWNER1,
    principal_type: str = "owner",
    role: str = "owner",
) -> None:
    with conn.cursor() as cur:
        for key, value in (
            ("app.current_org_id", str(org_id)),
            ("app.current_user_id", str(user_id)),
            ("app.current_principal_type", principal_type),
            ("app.current_role", role),
            ("app.current_gym_id", ""),
        ):
            cur.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)",
                (key, value),
            )


def _set_maintenance_context(conn, value: str = "platform") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.set_config('app.internal_maintenance', %s, true)",
            (value,),
        )


def _enqueue(
    conn,
    upload_id: str,
    *,
    asset_type: str = "logo",
    focal_y: float | None = None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT app_secure.enqueue_organization_asset_job(%s, %s, %s, %s)
            """,
            (asset_type, upload_id, focal_y, "127.0.0.1"),
        )
        return cur.fetchone()[0]


def _claim(conn, job_id: uuid.UUID, lease_token: uuid.UUID):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT organization_id, asset_type, upload_id, focal_y,
                   request_ip, requested_by_owner_id, attempt_count
            FROM app_secure.claim_organization_asset_job(%s, %s, 120)
            """,
            (job_id, lease_token),
        )
        return cur.fetchone()


def _finalize(conn, job_id: uuid.UUID, lease_token: uuid.UUID):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT applied, old_keys
            FROM app_secure.finalize_organization_asset_job(
                %s, %s, 800, 800, 4096, 'image/png'
            )
            """,
            (job_id, lease_token),
        )
        return cur.fetchone()


def _reset_state() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.owners SET org_id = %s WHERE id = %s",
                (_ORG1, _OWNER1),
            )
            cur.execute(
                "UPDATE public.owners SET org_id = %s WHERE id = %s",
                (_ORG2, _OWNER2),
            )
            cur.execute(
                "DELETE FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id IN (%s, %s)",
                (_ORG1, _ORG2),
            )
            cur.execute(
                "DELETE FROM public.organization_asset_jobs "
                "WHERE organization_id IN (%s, %s)",
                (_ORG1, _ORG2),
            )
            cur.execute(
                "DELETE FROM public.organization_asset_audit "
                "WHERE org_id IN (%s, %s)",
                (_ORG1, _ORG2),
            )
            cur.execute(
                """
                UPDATE public.organizations
                SET logo_key = 'legacy/logo-original',
                    logo_thumb_key = 'legacy/logo-thumb',
                    logo_medium_key = 'legacy/logo-medium',
                    logo_full_key = 'legacy/logo-full',
                    logo_meta = '{}'::jsonb,
                    logo_status = 'ready',
                    cover_key = 'legacy/cover-original',
                    cover_mobile_key = 'legacy/cover-mobile',
                    cover_tablet_key = 'legacy/cover-tablet',
                    cover_desktop_key = 'legacy/cover-desktop',
                    cover_meta = '{}'::jsonb,
                    cover_status = 'ready'
                WHERE id = %s
                """,
                (_ORG1,),
            )
            # The reset UPDATE correctly fires the durable cleanup trigger; those
            # rows are fixture noise and are removed before each assertion.
            cur.execute(
                "DELETE FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id = %s",
                (_ORG1,),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_asset_state() -> None:
    _reset_state()


def test_asset_and_cleanup_execute_acl_matrix_and_no_direct_tables() -> None:
    expected = {
        _ENQUEUE: {_APP_LOGIN},
        _CLAIM: {_WORKER_LOGIN},
        _FINALIZE: {_WORKER_LOGIN},
        _FAIL: {_WORKER_LOGIN},
        _DELETE: {_APP_LOGIN},
        _ASSET_DISPATCH: {_MAINTENANCE_LOGIN},
        _CLEANUP_CLAIM: {_WORKER_LOGIN},
        _CLEANUP_COMPLETE: {_WORKER_LOGIN},
        _CLEANUP_FAIL: {_WORKER_LOGIN},
        _CLEANUP_DISPATCH: {_MAINTENANCE_LOGIN},
    }
    logins = (
        (_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD"),
        (_AUTH_LOGIN, "AUTH_RUNTIME_PASSWORD"),
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
        (_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD"),
    )
    for login, env_name in logins:
        with _connect(login, env_name, autocommit=True) as conn:
            with conn.cursor() as cur:
                for signature, allowed in expected.items():
                    cur.execute(
                        "SELECT pg_catalog.has_function_privilege(current_user, %s, 'EXECUTE')",
                        (signature,),
                    )
                    assert cur.fetchone()[0] is (login in allowed)
                for relation in (
                    "public.organization_asset_jobs",
                    "public.organization_asset_cleanup_jobs",
                ):
                    for privilege in (
                        "SELECT",
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                    ):
                        cur.execute(
                            "SELECT pg_catalog.has_table_privilege(current_user, %s, %s)",
                            (relation, privilege),
                        )
                        assert cur.fetchone()[0] is False


def test_enqueue_requires_live_owner_binding_and_is_idempotent_under_concurrency() -> None:
    upload_id = uuid.uuid4().hex

    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_app_context(conn, principal_type="organization_user", role="admin")
            with pytest.raises(InsufficientPrivilege):
                _enqueue(conn, upload_id)

    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as conn:
        with conn.transaction():
            _set_app_context(conn, user_id=_OWNER2)
            with pytest.raises(InsufficientPrivilege):
                _enqueue(conn, upload_id)

    barrier = threading.Barrier(2)

    def invoke() -> uuid.UUID:
        conn = _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD")
        try:
            _set_app_context(conn)
            barrier.wait(timeout=5)
            job_id = _enqueue(conn, upload_id)
            conn.commit()
            return job_id
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: invoke(), range(2)))
    assert ids[0] == ids[1]

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.organization_asset_jobs "
                "WHERE organization_id = %s AND upload_id = %s::uuid",
                (_ORG1, upload_id),
            )
            assert cur.fetchone()[0] == 1


def test_claim_contention_supersession_and_stale_finalize_are_fenced() -> None:
    first_upload = uuid.uuid4().hex
    second_upload = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        first_job = _enqueue(app, first_upload)
        app.commit()

    token1 = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, first_job, token1) is not None
        worker.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, first_job, uuid.uuid4()) is None
        worker.commit()

    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        second_job = _enqueue(app, second_upload)
        app.commit()
    assert second_job != first_job

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _finalize(worker, first_job, token1)[0] is False
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT status, lease_token FROM public.organization_asset_jobs WHERE id = %s",
                (first_job,),
            )
            assert cur.fetchone() == ("superseded", None)
            cur.execute(
                "SELECT s3_key FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id = %s",
                (_ORG1,),
            )
            cleanup_keys = {row[0] for row in cur.fetchall()}
    assert f"quarantine/{_ORG1}/{first_upload}" in cleanup_keys
    assert f"logos/{_ORG1}/{first_upload}_full.webp" in cleanup_keys


def test_finalize_derives_keys_atomically_and_persists_cleanup_intents() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()

    lease = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        claimed = _claim(worker, job_id, lease)
        assert claimed is not None
        worker.commit()
        applied, old_keys = _finalize(worker, job_id, lease)
        assert applied is True
        assert set(old_keys) == {
            "legacy/logo-original",
            "legacy/logo-thumb",
            "legacy/logo-medium",
            "legacy/logo-full",
        }
        worker.commit()

    expected_original = f"originals/{_ORG1}/{upload_id}_original"
    expected_thumb = f"logos/{_ORG1}/{upload_id}_thumb.webp"
    expected_medium = f"logos/{_ORG1}/{upload_id}_medium.webp"
    expected_full = f"logos/{_ORG1}/{upload_id}_full.webp"
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT logo_key, logo_thumb_key, logo_medium_key, logo_full_key,
                       logo_status, logo_updated_by
                FROM public.organizations WHERE id = %s
                """,
                (_ORG1,),
            )
            assert cur.fetchone() == (
                expected_original,
                expected_thumb,
                expected_medium,
                expected_full,
                "ready",
                _OWNER1,
            )
            cur.execute(
                "SELECT status FROM public.organization_asset_jobs WHERE id = %s",
                (job_id,),
            )
            assert cur.fetchone()[0] == "completed"
            cur.execute(
                "SELECT s3_key FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id = %s",
                (_ORG1,),
            )
            cleanup_keys = {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT action, changed_by, new_s3_key, action_detail "
                "FROM public.organization_asset_audit "
                "WHERE org_id = %s AND action = 'uploaded'",
                (_ORG1,),
            )
            audit = cur.fetchone()

    assert {
        "legacy/logo-original",
        "legacy/logo-thumb",
        "legacy/logo-medium",
        "legacy/logo-full",
        f"quarantine/{_ORG1}/{upload_id}",
    }.issubset(cleanup_keys)
    assert audit[0] == "uploaded"
    assert audit[1] == _OWNER1
    assert audit[2] == expected_original
    assert audit[3]["source"] == "p3e_fenced_worker"

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _finalize(worker, job_id, lease)[0] is False
        worker.commit()


def test_live_owner_is_revalidated_at_claim() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE public.owners SET org_id = %s WHERE id = %s",
                (_ORG2, _OWNER1),
            )
        admin.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, job_id, uuid.uuid4()) is None
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT status, failure_code FROM public.organization_asset_jobs WHERE id = %s",
                (job_id,),
            )
            assert cur.fetchone() == ("cancelled", "owner_membership_revoked")


def test_delete_cancels_active_lease_and_stale_worker_cannot_republish() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()

    lease = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, job_id, lease) is not None
        worker.commit()

    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        with app.cursor() as cur:
            cur.execute(
                "SELECT app_secure.delete_current_organization_asset('logo')"
            )
            returned = set(cur.fetchone()[0])
        app.commit()
    assert returned == {
        "legacy/logo-original",
        "legacy/logo-thumb",
        "legacy/logo-medium",
        "legacy/logo-full",
    }

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _finalize(worker, job_id, lease)[0] is False
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT status FROM public.organization_asset_jobs WHERE id = %s",
                (job_id,),
            )
            assert cur.fetchone()[0] == "cancelled"
            cur.execute(
                "SELECT logo_key, logo_thumb_key, logo_medium_key, logo_full_key, logo_status "
                "FROM public.organizations WHERE id = %s",
                (_ORG1,),
            )
            assert cur.fetchone() == (None, None, None, None, None)
            cur.execute(
                "SELECT s3_key FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id = %s",
                (_ORG1,),
            )
            keys = {row[0] for row in cur.fetchall()}
    assert "legacy/logo-original" in keys
    assert f"quarantine/{_ORG1}/{upload_id}" in keys
    assert f"logos/{_ORG1}/{upload_id}_thumb.webp" in keys


def test_cleanup_claim_is_fenced_and_s3_key_cannot_be_supplied_by_worker() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()
    lease = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, job_id, lease) is not None
        worker.commit()
        assert _finalize(worker, job_id, lease)[0] is True
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT id, s3_key
                FROM public.organization_asset_cleanup_jobs
                WHERE organization_id = %s AND not_before <= pg_catalog.clock_timestamp()
                ORDER BY created_at, id LIMIT 1
                """,
                (_ORG1,),
            )
            cleanup_id, expected_key = cur.fetchone()

    first_token = uuid.uuid4()
    second_token = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        with worker.cursor() as cur:
            cur.execute(
                "SELECT app_secure.claim_organization_asset_cleanup(%s, %s, 60)",
                (cleanup_id, first_token),
            )
            assert cur.fetchone()[0] == expected_key
        worker.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        with worker.cursor() as cur:
            cur.execute(
                "SELECT app_secure.claim_organization_asset_cleanup(%s, %s, 60)",
                (cleanup_id, second_token),
            )
            assert cur.fetchone()[0] is None
        worker.commit()

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        with worker.cursor() as cur:
            cur.execute(
                "SELECT app_secure.complete_organization_asset_cleanup(%s, %s)",
                (cleanup_id, first_token),
            )
            assert cur.fetchone()[0] is True
        worker.commit()


def test_maintenance_dispatchers_require_exact_context_and_bounds() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE public.organization_asset_jobs "
                "SET last_dispatched_at = pg_catalog.clock_timestamp() - interval '1 minute' "
                "WHERE id = %s",
                (job_id,),
            )
        admin.commit()

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as maintenance:
        with pytest.raises(InsufficientPrivilege):
            with maintenance.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.dispatchable_organization_asset_jobs(1)")
                cur.fetchall()
        maintenance.rollback()

        _set_maintenance_context(maintenance)
        with maintenance.cursor() as cur:
            cur.execute("SELECT job_id FROM app_secure.dispatchable_organization_asset_jobs(10)")
            assert job_id in {row[0] for row in cur.fetchall()}
        maintenance.commit()

    for invalid in (0, 101):
        with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as maintenance:
            _set_maintenance_context(maintenance)
            with pytest.raises(InsufficientPrivilege):
                with maintenance.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM app_secure.dispatchable_organization_asset_jobs(%s)",
                        (invalid,),
                    )
                    cur.fetchall()
            maintenance.rollback()


def test_cleanup_retry_returns_to_pending_and_dispatch_context_does_not_leak() -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_app_context(app)
        job_id = _enqueue(app, upload_id)
        app.commit()
    lease = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        assert _claim(worker, job_id, lease) is not None
        worker.commit()
        assert _finalize(worker, job_id, lease)[0] is True
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT id FROM public.organization_asset_cleanup_jobs "
                "WHERE organization_id = %s AND not_before <= pg_catalog.clock_timestamp() "
                "ORDER BY created_at, id LIMIT 1",
                (_ORG1,),
            )
            cleanup_id = cur.fetchone()[0]

    token = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        with worker.cursor() as cur:
            cur.execute(
                "SELECT app_secure.claim_organization_asset_cleanup(%s, %s, 60)",
                (cleanup_id, token),
            )
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT app_secure.fail_organization_asset_cleanup(%s, %s, 's3_delete_error')",
                (cleanup_id, token),
            )
            assert cur.fetchone()[0] == "pending"
        worker.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE public.organization_asset_cleanup_jobs "
                "SET last_dispatched_at = NULL, not_before = pg_catalog.clock_timestamp() "
                "WHERE id = %s",
                (cleanup_id,),
            )
        admin.commit()

    with _connect(_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD") as maintenance:
        _set_maintenance_context(maintenance)
        with maintenance.cursor() as cur:
            cur.execute(
                "SELECT cleanup_id FROM app_secure.dispatchable_organization_asset_cleanup(200)"
            )
            assert cleanup_id in {row[0] for row in cur.fetchall()}
        maintenance.commit()
        with pytest.raises(InsufficientPrivilege):
            with maintenance.cursor() as cur:
                cur.execute(
                    "SELECT cleanup_id FROM app_secure.dispatchable_organization_asset_cleanup(1)"
                )
                cur.fetchall()
        maintenance.rollback()
