from __future__ import annotations

import os
import uuid

import psycopg
import pytest


_DB_HOST = "127.0.0.1"
_DB_NAME = "gymflow_test"
_ADMIN_LOGIN = "migration_owner"
_APP_LOGIN = "app_test_runtime"
_WORKER_LOGIN = "worker_test_runtime"

_ORG = uuid.UUID("e3200000-0000-4000-8000-000000000001")
_OWNER = uuid.UUID("e3200000-0000-4000-8000-000000000011")


def _connect(login: str, password_env: str):
    return psycopg.connect(
        host=_DB_HOST,
        dbname=_DB_NAME,
        user=login,
        password=os.environ[password_env],
    )


def _set_owner_context(conn) -> None:
    with conn.cursor() as cur:
        for key, value in (
            ("app.current_org_id", str(_ORG)),
            ("app.current_user_id", str(_OWNER)),
            ("app.current_principal_type", "owner"),
            ("app.current_role", "owner"),
            ("app.current_gym_id", ""),
        ):
            cur.execute("SELECT pg_catalog.set_config(%s, %s, true)", (key, value))


def _enqueue(conn, asset_type: str, upload_id: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT app_secure.enqueue_organization_asset_job(%s, %s, %s, %s)",
            (asset_type, upload_id, 0.5 if asset_type == "cover" else None, "127.0.0.1"),
        )
        return cur.fetchone()[0]


def _claim(conn, job_id: uuid.UUID, lease: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id FROM (SELECT %s::uuid AS job_id) AS expected "
            "WHERE EXISTS (SELECT 1 FROM app_secure.claim_organization_asset_job(%s, %s, 120))",
            (job_id, job_id, lease),
        )
        assert cur.fetchone()[0] == job_id


def _finalize(conn, job_id: uuid.UUID, lease: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied FROM app_secure.finalize_organization_asset_job("
            "%s, %s, 800, 800, 4096, 'image/png')",
            (job_id, lease),
        )
        assert cur.fetchone()[0] is True


def _reset() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.organization_asset_cleanup_jobs WHERE organization_id = %s", (_ORG,))
            cur.execute("DELETE FROM public.organization_asset_jobs WHERE organization_id = %s", (_ORG,))
            cur.execute("DELETE FROM public.organization_asset_audit WHERE org_id = %s", (_ORG,))
            cur.execute(
                """
                UPDATE public.organizations
                SET logo_key = 'legacy/logo-original',
                    logo_thumb_key = 'legacy/logo-thumb',
                    logo_medium_key = 'legacy/logo-medium',
                    logo_full_key = 'legacy/logo-full',
                    logo_meta = '{}'::jsonb,
                    logo_status = 'ready',
                    logo_updated_by = NULL,
                    logo_updated_by_owner_id = NULL,
                    cover_key = 'legacy/cover-original',
                    cover_mobile_key = 'legacy/cover-mobile',
                    cover_tablet_key = 'legacy/cover-tablet',
                    cover_desktop_key = 'legacy/cover-desktop',
                    cover_meta = '{}'::jsonb,
                    cover_status = 'ready',
                    cover_updated_by = NULL,
                    cover_updated_by_owner_id = NULL
                WHERE id = %s
                """,
                (_ORG,),
            )
            cur.execute("DELETE FROM public.organization_asset_cleanup_jobs WHERE organization_id = %s", (_ORG,))
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    _reset()


def test_legacy_and_modern_branding_actor_foreign_keys_coexist() -> None:
    expected = {
        "organizations_logo_updated_by_fkey": ("organizations", "logo_updated_by", "gym_owners"),
        "organizations_cover_updated_by_fkey": ("organizations", "cover_updated_by", "gym_owners"),
        "organization_asset_audit_changed_by_fkey": ("organization_asset_audit", "changed_by", "gym_owners"),
        "organizations_logo_updated_by_owner_fkey": ("organizations", "logo_updated_by_owner_id", "owners"),
        "organizations_cover_updated_by_owner_fkey": ("organizations", "cover_updated_by_owner_id", "owners"),
        "organization_asset_audit_changed_by_owner_fkey": ("organization_asset_audit", "changed_by_owner_id", "owners"),
    }
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            for constraint_name, wanted in expected.items():
                cur.execute(
                    """
                    SELECT source.relname, source_attribute.attname, target.relname
                    FROM pg_catalog.pg_constraint AS c
                    JOIN pg_catalog.pg_class AS source ON source.oid = c.conrelid
                    JOIN pg_catalog.pg_class AS target ON target.oid = c.confrelid
                    JOIN pg_catalog.pg_attribute AS source_attribute
                      ON source_attribute.attrelid = source.oid
                     AND source_attribute.attnum = c.conkey[1]
                    WHERE c.conname = %s AND c.contype = 'f' AND c.confdeltype = 'n'
                    """,
                    (constraint_name,),
                )
                assert cur.fetchone() == wanted
            cur.execute(
                "SELECT pg_catalog.pg_get_constraintdef(oid) "
                "FROM pg_catalog.pg_constraint "
                "WHERE conname = 'ck_organization_asset_audit_single_actor_domain'"
            )
            assert "changed_by IS NULL" in cur.fetchone()[0]


@pytest.mark.parametrize("asset_type", ["logo", "cover"])
def test_finalize_records_modern_owner_without_impersonating_legacy_staff(asset_type: str) -> None:
    upload_id = uuid.uuid4().hex
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_owner_context(app)
        job_id = _enqueue(app, asset_type, upload_id)
        app.commit()

    lease = uuid.uuid4()
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as worker:
        _claim(worker, job_id, lease)
        worker.commit()
        _finalize(worker, job_id, lease)
        worker.commit()

    legacy_column = f"{asset_type}_updated_by"
    owner_column = f"{asset_type}_updated_by_owner_id"
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                f"SELECT {legacy_column}, {owner_column} FROM public.organizations WHERE id = %s",
                (_ORG,),
            )
            assert cur.fetchone() == (None, _OWNER)
            cur.execute(
                """
                SELECT changed_by, changed_by_owner_id, action, action_detail
                FROM public.organization_asset_audit
                WHERE org_id = %s AND asset_type = %s AND action = 'uploaded'
                ORDER BY created_at DESC LIMIT 1
                """,
                (_ORG, asset_type),
            )
            changed_by, changed_by_owner_id, action, detail = cur.fetchone()
    assert changed_by is None
    assert changed_by_owner_id == _OWNER
    assert action == "uploaded"
    assert detail["source"] == "p3e_fenced_worker"


def test_delete_records_modern_owner_without_impersonating_legacy_staff() -> None:
    with _connect(_APP_LOGIN, "GENERAL_RUNTIME_PASSWORD") as app:
        _set_owner_context(app)
        with app.cursor() as cur:
            cur.execute("SELECT app_secure.delete_current_organization_asset('logo')")
            assert "legacy/logo-original" in set(cur.fetchone()[0])
        app.commit()

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT logo_updated_by, logo_updated_by_owner_id "
                "FROM public.organizations WHERE id = %s",
                (_ORG,),
            )
            assert cur.fetchone() == (None, _OWNER)
            cur.execute(
                """
                SELECT changed_by, changed_by_owner_id
                FROM public.organization_asset_audit
                WHERE org_id = %s AND asset_type = 'logo' AND action = 'deleted'
                ORDER BY created_at DESC LIMIT 1
                """,
                (_ORG,),
            )
            assert cur.fetchone() == (None, _OWNER)
