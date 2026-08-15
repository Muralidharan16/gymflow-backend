"""P3E: durable cleanup intents for organization asset side effects.

Revision ID: p07d8e9f0a30
Revises: o07d8e9f0a2f
Create Date: 2026-08-15

Database publication and S3 deletion cannot be atomic. This revision closes
that gap without restoring an arbitrary-key queue primitive: triggers persist
cleanup intents whenever authoritative organization keys are replaced/cleared
or an asset job reaches a terminal state. A bounded worker capability exposes
exactly one persisted S3 key per leased cleanup job, while maintenance may only
redispatch opaque cleanup-job IDs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "p07d8e9f0a30"
down_revision = "o07d8e9f0a2f"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER_ROLE = "worker_runtime"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_CLEANUP_TABLE = "public.organization_asset_cleanup_jobs"

_CLAIM = "app_secure.claim_organization_asset_cleanup(uuid,uuid,integer)"
_COMPLETE = "app_secure.complete_organization_asset_cleanup(uuid,uuid)"
_FAIL = "app_secure.fail_organization_asset_cleanup(uuid,uuid,text)"
_DISPATCH = "app_secure.dispatchable_organization_asset_cleanup(integer)"
_TRIGGER_FUNCTIONS = (
    "app_secure.capture_organization_asset_key_cleanup()",
    "app_secure.capture_organization_asset_job_cleanup()",
)


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E asset cleanup migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _direct_table_privileges(bind, relation: str, role_name: str) -> set[str]:
    return {
        str(value)
        for value in bind.execute(sa.text("""
            SELECT DISTINCT acl.privilege_type::text
            FROM pg_catalog.pg_class AS relation_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
              AND grantee.rolname = :role_name
        """), {"relation": relation, "role_name": role_name}).scalars().all()
    }


def _require_predecessor(bind) -> None:
    if bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"
    ), {"relation": _CLEANUP_TABLE}).scalar_one():
        raise RuntimeError("organization_asset_cleanup_jobs already exists")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass('public.organization_asset_jobs') IS NOT NULL"
    )).scalar_one():
        raise RuntimeError("P3E asset job predecessor is missing")


def _require_forward(bind) -> None:
    owner = bind.execute(sa.text("""
        SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text
        FROM pg_catalog.pg_class AS relation_data
        WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
    """), {"relation": _CLEANUP_TABLE}).scalar_one_or_none()
    if owner != _MIGRATION_OWNER:
        raise RuntimeError("organization_asset_cleanup_jobs owner drifted")
    for role_name in (
        "app_runtime", "auth_runtime", _WORKER_ROLE, _MAINTENANCE_ROLE
    ):
        if _direct_table_privileges(bind, _CLEANUP_TABLE, role_name):
            raise RuntimeError(f"{role_name} gained direct asset cleanup table ACL")

    expected = {
        "claim_organization_asset_cleanup": (_WORKER_ROLE,),
        "complete_organization_asset_cleanup": (_WORKER_ROLE,),
        "fail_organization_asset_cleanup": (_WORKER_ROLE,),
        "dispatchable_organization_asset_cleanup": (_MAINTENANCE_ROLE,),
    }
    for name, allowed in expected.items():
        row = bind.execute(sa.text("""
            SELECT owner.rolname::text AS owner_name,
                   procedure.prosecdef,
                   procedure.proconfig,
                   pg_catalog.has_function_privilege(
                       'worker_runtime', procedure.oid, 'EXECUTE'
                   ) AS worker_execute,
                   pg_catalog.has_function_privilege(
                       'lifecycle_maintenance_runtime', procedure.oid, 'EXECUTE'
                   ) AS maintenance_execute,
                   pg_catalog.has_function_privilege(
                       'app_runtime', procedure.oid, 'EXECUTE'
                   ) AS api_execute,
                   pg_catalog.has_function_privilege(
                       'auth_runtime', procedure.oid, 'EXECUTE'
                   ) AS auth_execute,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.aclexplode(
                           COALESCE(
                               procedure.proacl,
                               pg_catalog.acldefault('f', procedure.proowner)
                           )
                       ) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'app_secure'
              AND procedure.proname = :name
              AND procedure.prokind = 'f'
        """), {"name": name}).mappings().one_or_none()
        if row is None:
            raise RuntimeError(f"asset cleanup capability missing: {name}")
        if (
            row["owner_name"] != _SECURITY_OWNER
            or not bool(row["prosecdef"])
            or set(row["proconfig"] or [])
            != {"search_path=pg_catalog", "row_security=on"}
            or bool(row["api_execute"])
            or bool(row["auth_execute"])
            or bool(row["public_execute"])
            or bool(row["worker_execute"]) != (_WORKER_ROLE in allowed)
            or bool(row["maintenance_execute"]) != (_MAINTENANCE_ROLE in allowed)
        ):
            raise RuntimeError(f"asset cleanup EXECUTE boundary drift: {name}")

    trigger_rows = bind.execute(sa.text("""
        SELECT procedure.proname,
               owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   'migration_owner', procedure.oid, 'EXECUTE'
               ) AS migration_execute,
               EXISTS (
                   SELECT 1 FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )
                   ) AS acl
                   WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname IN (
              'capture_organization_asset_key_cleanup',
              'capture_organization_asset_job_cleanup'
          )
    """)).mappings().all()
    if len(trigger_rows) != 2:
        raise RuntimeError("asset cleanup trigger functions are missing")
    for row in trigger_rows:
        if (
            row["owner_name"] != _SECURITY_OWNER
            or not bool(row["prosecdef"])
            or set(row["proconfig"] or [])
            != {"search_path=pg_catalog", "row_security=on"}
            or bool(row["migration_execute"])
            or bool(row["public_execute"])
        ):
            raise RuntimeError("asset cleanup trigger function ACL drift")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("""
        CREATE TABLE public.organization_asset_cleanup_jobs (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL
                REFERENCES public.organizations(id) ON DELETE CASCADE,
            s3_key text NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
            lease_token uuid NULL,
            lease_expires_at timestamptz NULL,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            failure_code text NULL,
            last_dispatched_at timestamptz NULL,
            not_before timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            completed_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT ck_organization_asset_cleanup_lease_pair
                CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
        )
    """)
    op.execute("""
        CREATE INDEX ix_organization_asset_cleanup_dispatch
        ON public.organization_asset_cleanup_jobs (
            status, not_before, last_dispatched_at, lease_expires_at, created_at
        ) WHERE status IN ('pending', 'processing')
    """)
    op.execute("""
        REVOKE ALL ON TABLE public.organization_asset_cleanup_jobs
        FROM PUBLIC, app_runtime, auth_runtime, worker_runtime,
             lifecycle_maintenance_runtime
    """)
    op.execute("""
        GRANT SELECT (
            id, organization_id, s3_key, status, lease_token, lease_expires_at,
            attempt_count, failure_code, last_dispatched_at, not_before,
            completed_at, created_at, updated_at
        ), INSERT (
            id, organization_id, s3_key, status, attempt_count, not_before,
            created_at, updated_at
        ), UPDATE (
            status, lease_token, lease_expires_at, attempt_count, failure_code,
            last_dispatched_at, completed_at, updated_at
        ) ON TABLE public.organization_asset_cleanup_jobs TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.capture_organization_asset_key_cleanup()
        RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_key text;
            v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            FOR v_key IN
                SELECT candidate.key
                FROM pg_catalog.unnest(ARRAY[
                    CASE WHEN OLD.logo_key IS DISTINCT FROM NEW.logo_key THEN OLD.logo_key END,
                    CASE WHEN OLD.logo_thumb_key IS DISTINCT FROM NEW.logo_thumb_key THEN OLD.logo_thumb_key END,
                    CASE WHEN OLD.logo_medium_key IS DISTINCT FROM NEW.logo_medium_key THEN OLD.logo_medium_key END,
                    CASE WHEN OLD.logo_full_key IS DISTINCT FROM NEW.logo_full_key THEN OLD.logo_full_key END,
                    CASE WHEN OLD.cover_key IS DISTINCT FROM NEW.cover_key THEN OLD.cover_key END,
                    CASE WHEN OLD.cover_mobile_key IS DISTINCT FROM NEW.cover_mobile_key THEN OLD.cover_mobile_key END,
                    CASE WHEN OLD.cover_tablet_key IS DISTINCT FROM NEW.cover_tablet_key THEN OLD.cover_tablet_key END,
                    CASE WHEN OLD.cover_desktop_key IS DISTINCT FROM NEW.cover_desktop_key THEN OLD.cover_desktop_key END
                ]::text[]) AS candidate(key)
                WHERE candidate.key IS NOT NULL
            LOOP
                INSERT INTO public.organization_asset_cleanup_jobs (
                    id, organization_id, s3_key, status, attempt_count,
                    not_before, created_at, updated_at
                ) VALUES (
                    pg_catalog.gen_random_uuid(), NEW.id, v_key,
                    'pending', 0, v_now, v_now, v_now
                );
            END LOOP;
            RETURN NEW;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.capture_organization_asset_job_cleanup()
        RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_key text;
            v_upload_hex text;
            v_not_before timestamptz;
            v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF NEW.status IS NOT DISTINCT FROM OLD.status
               OR NEW.status NOT IN ('completed', 'failed', 'superseded', 'cancelled') THEN
                RETURN NEW;
            END IF;
            v_upload_hex := pg_catalog.replace(NEW.upload_id::text, '-', '');
            v_not_before := CASE
                WHEN NEW.status = 'completed' THEN v_now
                ELSE GREATEST(
                    v_now,
                    COALESCE(OLD.lease_expires_at, v_now) + interval '30 seconds'
                )
            END;
            INSERT INTO public.organization_asset_cleanup_jobs (
                id, organization_id, s3_key, status, attempt_count,
                not_before, created_at, updated_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), NEW.organization_id,
                'quarantine/' || NEW.organization_id::text || '/' || v_upload_hex,
                'pending', 0, v_not_before, v_now, v_now
            );
            IF NEW.status <> 'completed' THEN
                FOR v_key IN
                    SELECT candidate.key
                    FROM pg_catalog.unnest(
                        CASE WHEN NEW.asset_type = 'logo' THEN ARRAY[
                            'originals/' || NEW.organization_id::text || '/' || v_upload_hex || '_original',
                            'logos/' || NEW.organization_id::text || '/' || v_upload_hex || '_thumb.webp',
                            'logos/' || NEW.organization_id::text || '/' || v_upload_hex || '_medium.webp',
                            'logos/' || NEW.organization_id::text || '/' || v_upload_hex || '_full.webp'
                        ]::text[] ELSE ARRAY[
                            'originals/' || NEW.organization_id::text || '/' || v_upload_hex || '_original',
                            'covers/' || NEW.organization_id::text || '/' || v_upload_hex || '_mobile.webp',
                            'covers/' || NEW.organization_id::text || '/' || v_upload_hex || '_tablet.webp',
                            'covers/' || NEW.organization_id::text || '/' || v_upload_hex || '_desktop.webp'
                        ]::text[] END
                    ) AS candidate(key)
                LOOP
                    INSERT INTO public.organization_asset_cleanup_jobs (
                        id, organization_id, s3_key, status, attempt_count,
                        not_before, created_at, updated_at
                    ) VALUES (
                        pg_catalog.gen_random_uuid(), NEW.organization_id, v_key,
                        'pending', 0, v_not_before, v_now, v_now
                    );
                END LOOP;
            END IF;
            RETURN NEW;
        END;
        $function$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
            app_secure.capture_organization_asset_key_cleanup(),
            app_secure.capture_organization_asset_job_cleanup()
        FROM PUBLIC
    """)
    # migration_owner owns the trigger target tables but not these reduced-owner
    # functions. Grant EXECUTE only for trigger installation, then revoke it.
    op.execute("""
        GRANT EXECUTE ON FUNCTION
            app_secure.capture_organization_asset_key_cleanup(),
            app_secure.capture_organization_asset_job_cleanup()
        TO migration_owner
    """)
    op.execute("RESET ROLE")

    op.execute("""
        CREATE TRIGGER trg_organization_asset_key_cleanup
        AFTER UPDATE OF
            logo_key, logo_thumb_key, logo_medium_key, logo_full_key,
            cover_key, cover_mobile_key, cover_tablet_key, cover_desktop_key
        ON public.organizations
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.capture_organization_asset_key_cleanup()
    """)
    op.execute("""
        CREATE TRIGGER trg_organization_asset_job_cleanup
        AFTER UPDATE OF status
        ON public.organization_asset_jobs
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.capture_organization_asset_job_cleanup()
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("""
        REVOKE EXECUTE ON FUNCTION
            app_secure.capture_organization_asset_key_cleanup(),
            app_secure.capture_organization_asset_job_cleanup()
        FROM migration_owner
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.claim_organization_asset_cleanup(
            p_cleanup_id uuid,
            p_lease_token uuid,
            p_lease_seconds integer
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF p_lease_token IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 300 THEN
                RAISE EXCEPTION 'invalid organization asset cleanup lease'
                    USING ERRCODE = '42501';
            END IF;
            SELECT cleanup.* INTO v_job
            FROM public.organization_asset_cleanup_jobs AS cleanup
            WHERE cleanup.id = p_cleanup_id FOR UPDATE;
            IF NOT FOUND OR v_job.not_before > v_now
               OR v_job.status NOT IN ('pending', 'processing')
               OR (v_job.status = 'processing'
                   AND v_job.lease_expires_at IS NOT NULL
                   AND v_job.lease_expires_at > v_now) THEN
                RETURN NULL;
            END IF;
            UPDATE public.organization_asset_cleanup_jobs
            SET status = 'processing', lease_token = p_lease_token,
                lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
                attempt_count = attempt_count + 1, failure_code = NULL,
                updated_at = v_now
            WHERE id = p_cleanup_id;
            RETURN v_job.s3_key;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.complete_organization_asset_cleanup(
            p_cleanup_id uuid,
            p_lease_token uuid
        ) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            UPDATE public.organization_asset_cleanup_jobs AS cleanup
            SET status = 'completed', lease_token = NULL, lease_expires_at = NULL,
                failure_code = NULL, completed_at = v_now, updated_at = v_now
            WHERE cleanup.id = p_cleanup_id
              AND cleanup.status = 'processing'
              AND cleanup.lease_token = p_lease_token
              AND cleanup.lease_expires_at > v_now;
            RETURN FOUND;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.fail_organization_asset_cleanup(
            p_cleanup_id uuid,
            p_lease_token uuid,
            p_failure_code text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_next_status text;
        BEGIN
            IF p_failure_code IS DISTINCT FROM 's3_delete_error' THEN
                RAISE EXCEPTION 'invalid organization asset cleanup failure'
                    USING ERRCODE = '42501';
            END IF;
            SELECT cleanup.* INTO v_job
            FROM public.organization_asset_cleanup_jobs AS cleanup
            WHERE cleanup.id = p_cleanup_id FOR UPDATE;
            IF NOT FOUND OR v_job.status <> 'processing'
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= v_now THEN
                RETURN NULL;
            END IF;
            v_next_status := CASE WHEN v_job.attempt_count < 10 THEN 'pending' ELSE 'failed' END;
            UPDATE public.organization_asset_cleanup_jobs
            SET status = v_next_status, lease_token = NULL, lease_expires_at = NULL,
                failure_code = p_failure_code,
                completed_at = CASE WHEN v_next_status = 'failed' THEN v_now ELSE NULL END,
                updated_at = v_now
            WHERE id = p_cleanup_id;
            RETURN v_next_status;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.dispatchable_organization_asset_cleanup(
            p_batch_size integer
        ) RETURNS TABLE (cleanup_id uuid)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true)
                   IS DISTINCT FROM 'platform'
               OR p_batch_size < 1 OR p_batch_size > 200 THEN
                RAISE EXCEPTION 'invalid organization asset cleanup dispatch'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT cleanup.id
                FROM public.organization_asset_cleanup_jobs AS cleanup
                WHERE cleanup.not_before <= v_now
                  AND ((
                        cleanup.status = 'pending'
                        AND (cleanup.last_dispatched_at IS NULL
                             OR cleanup.last_dispatched_at <= v_now - interval '30 seconds')
                      ) OR (
                        cleanup.status = 'processing'
                        AND cleanup.lease_expires_at <= v_now
                        AND (cleanup.last_dispatched_at IS NULL
                             OR cleanup.last_dispatched_at <= v_now - interval '30 seconds')
                      ))
                ORDER BY cleanup.created_at, cleanup.id
                LIMIT p_batch_size FOR UPDATE SKIP LOCKED
            ), updated AS (
                UPDATE public.organization_asset_cleanup_jobs AS cleanup
                SET last_dispatched_at = v_now, updated_at = v_now
                FROM candidates WHERE cleanup.id = candidates.id
                RETURNING cleanup.id
            )
            SELECT updated.id FROM updated ORDER BY updated.id;
        END;
        $function$;
    """)

    for signature in (_CLAIM, _COMPLETE, _FAIL, _DISPATCH):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_CLAIM}, {_COMPLETE}, {_FAIL} TO {_WORKER_ROLE}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DISPATCH} TO {_MAINTENANCE_ROLE}")
    op.execute("RESET ROLE")
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)
    op.execute("DROP TRIGGER trg_organization_asset_job_cleanup ON public.organization_asset_jobs")
    op.execute("DROP TRIGGER trg_organization_asset_key_cleanup ON public.organizations")
    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in (_DISPATCH, _FAIL, _COMPLETE, _CLAIM):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP FUNCTION app_secure.capture_organization_asset_job_cleanup()")
    op.execute("DROP FUNCTION app_secure.capture_organization_asset_key_cleanup()")
    op.execute("RESET ROLE")
    op.execute("DROP TABLE public.organization_asset_cleanup_jobs")
    _require_predecessor(bind)
