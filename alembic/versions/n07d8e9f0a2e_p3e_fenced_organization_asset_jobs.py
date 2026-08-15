"""P3E: durable, fenced organization asset processing boundary.

Revision ID: n07d8e9f0a2e
Revises: m07d8e9f0a2d
Create Date: 2026-08-15

Branding uploads previously trusted Celery payload fields as authority, mutated
organizations directly through worker_runtime, generated fresh random S3 keys
on every retry, and accepted arbitrary queued key lists for deletion. This
revision moves authorization, retry and concurrency authority into PostgreSQL.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "n07d8e9f0a2e"
down_revision = "m07d8e9f0a2d"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API_ROLE = "app_runtime"
_WORKER_ROLE = "worker_runtime"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_AUTH_ROLE = "auth_runtime"
_TABLE = "public.organization_asset_jobs"

_ENQUEUE_SIGNATURE = "app_secure.enqueue_organization_asset_job(text,text,numeric,text)"
_CLAIM_SIGNATURE = "app_secure.claim_organization_asset_job(uuid,uuid,integer)"
_FINALIZE_SIGNATURE = (
    "app_secure.finalize_organization_asset_job(uuid,uuid,integer,integer,bigint,text)"
)
_FAIL_SIGNATURE = "app_secure.fail_organization_asset_job(uuid,uuid,text)"
_DISPATCH_SIGNATURE = "app_secure.dispatchable_organization_asset_jobs(integer)"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E asset migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(session_user, :role, 'SET')"),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


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


def _function_acl(bind, function_name: str):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   :api_role, procedure.oid, 'EXECUTE'
               ) AS api_execute,
               pg_catalog.has_function_privilege(
                   :worker_role, procedure.oid, 'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   :maintenance_role, procedure.oid, 'EXECUTE'
               ) AS maintenance_execute,
               pg_catalog.has_function_privilege(
                   :auth_role, procedure.oid, 'EXECUTE'
               ) AS auth_execute,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )
                   ) AS acl
                   WHERE acl.grantee = 0
                     AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = :function_name
          AND procedure.prokind = 'f'
    """), {
        "function_name": function_name,
        "api_role": _API_ROLE,
        "worker_role": _WORKER_ROLE,
        "maintenance_role": _MAINTENANCE_ROLE,
        "auth_role": _AUTH_ROLE,
    }).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    if bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"
    ), {"relation": _TABLE}).scalar_one():
        raise RuntimeError("organization_asset_jobs already exists")
    for name in (
        "enqueue_organization_asset_job",
        "claim_organization_asset_job",
        "finalize_organization_asset_job",
        "fail_organization_asset_job",
        "dispatchable_organization_asset_jobs",
    ):
        if _function_acl(bind, name) is not None:
            raise RuntimeError(f"P3E asset capability already exists: {name}")


def _require_forward(bind) -> None:
    owner = bind.execute(sa.text("""
        SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text
        FROM pg_catalog.pg_class AS relation_data
        WHERE relation_data.oid = pg_catalog.to_regclass(:relation)
    """), {"relation": _TABLE}).scalar_one_or_none()
    if owner != _MIGRATION_OWNER:
        raise RuntimeError("organization_asset_jobs owner drifted")
    for role_name in (_API_ROLE, _AUTH_ROLE, _WORKER_ROLE, _MAINTENANCE_ROLE):
        if _direct_table_privileges(bind, _TABLE, role_name):
            raise RuntimeError(f"{role_name} gained direct asset-job table ACL")

    expected = {
        "enqueue_organization_asset_job": (_API_ROLE,),
        "claim_organization_asset_job": (_WORKER_ROLE,),
        "finalize_organization_asset_job": (_WORKER_ROLE,),
        "fail_organization_asset_job": (_WORKER_ROLE,),
        "dispatchable_organization_asset_jobs": (_MAINTENANCE_ROLE,),
    }
    key_by_role = {
        _API_ROLE: "api_execute",
        _WORKER_ROLE: "worker_execute",
        _MAINTENANCE_ROLE: "maintenance_execute",
        _AUTH_ROLE: "auth_execute",
    }
    for function_name, allowed_roles in expected.items():
        row = _function_acl(bind, function_name)
        if row is None:
            raise RuntimeError(f"P3E asset capability missing: {function_name}")
        if (
            row["owner_name"] != _SECURITY_OWNER
            or not bool(row["prosecdef"])
            or row["volatility"] != "v"
            or set(row["proconfig"] or [])
            != {"search_path=pg_catalog", "row_security=on"}
            or bool(row["public_execute"])
        ):
            raise RuntimeError(f"P3E asset capability contract drift: {function_name}")
        for role_name, key in key_by_role.items():
            if bool(row[key]) != (role_name in allowed_roles):
                raise RuntimeError(
                    f"P3E asset EXECUTE ACL drift: {function_name}/{role_name}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("""
        CREATE TABLE public.organization_asset_jobs (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL
                REFERENCES public.organizations(id) ON DELETE CASCADE,
            requested_by_owner_id uuid NOT NULL
                REFERENCES public.owners(id) ON DELETE CASCADE,
            asset_type text NOT NULL CHECK (asset_type IN ('logo', 'cover')),
            upload_id uuid NOT NULL,
            focal_y numeric(5,4) NULL
                CHECK (focal_y IS NULL OR (focal_y >= 0 AND focal_y <= 1)),
            request_ip text NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'processing', 'completed', 'failed',
                    'superseded', 'cancelled'
                )),
            lease_token uuid NULL,
            lease_expires_at timestamptz NULL,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            failure_code text NULL,
            last_dispatched_at timestamptz NULL,
            completed_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_organization_asset_job_upload
                UNIQUE (organization_id, asset_type, upload_id),
            CONSTRAINT ck_organization_asset_job_lease_pair
                CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
        )
    """)
    op.execute("""
        CREATE INDEX ix_organization_asset_jobs_dispatch
        ON public.organization_asset_jobs (
            status, last_dispatched_at, lease_expires_at, created_at
        )
        WHERE status IN ('pending', 'processing')
    """)
    op.execute("""
        CREATE INDEX ix_organization_asset_jobs_org_asset
        ON public.organization_asset_jobs (
            organization_id, asset_type, created_at DESC
        )
    """)
    op.execute("""
        REVOKE ALL ON TABLE public.organization_asset_jobs
        FROM PUBLIC, app_runtime, auth_runtime, worker_runtime,
             lifecycle_maintenance_runtime
    """)
    op.execute("""
        GRANT SELECT (
            id, organization_id, requested_by_owner_id, asset_type, upload_id,
            focal_y, request_ip, status, lease_token, lease_expires_at,
            attempt_count, failure_code, last_dispatched_at, completed_at,
            created_at, updated_at
        ), INSERT (
            id, organization_id, requested_by_owner_id, asset_type, upload_id,
            focal_y, request_ip, status, attempt_count, last_dispatched_at,
            created_at, updated_at
        ), UPDATE (
            status, lease_token, lease_expires_at, attempt_count, failure_code,
            last_dispatched_at, completed_at, updated_at
        )
        ON TABLE public.organization_asset_jobs TO app_security_owner
    """)

    # P3A already grants derivative/status SELECT and organizations.updated_at
    # UPDATE. P3E adds only asset authority absent in the predecessor.
    op.execute("""
        GRANT SELECT (
            logo_key, logo_meta, logo_updated_at, logo_updated_by,
            cover_key, cover_meta, cover_updated_at, cover_updated_by
        ), UPDATE (
            logo_key, logo_thumb_key, logo_medium_key, logo_full_key,
            logo_meta, logo_status, logo_updated_at, logo_updated_by,
            cover_key, cover_mobile_key, cover_tablet_key, cover_desktop_key,
            cover_meta, cover_status, cover_updated_at, cover_updated_by
        )
        ON TABLE public.organizations TO app_security_owner
    """)
    op.execute("""
        GRANT INSERT (
            id, org_id, changed_by, asset_type, old_s3_key, new_s3_key,
            action, action_detail, ip_address
        )
        ON TABLE public.organization_asset_audit TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.enqueue_organization_asset_job(
            p_asset_type text,
            p_upload_id text,
            p_focal_y numeric,
            p_request_ip text
        ) RETURNS uuid
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_org_text text;
            v_user_text text;
            v_principal_type text;
            v_role text;
            v_gym text;
            v_org_id uuid;
            v_owner_id uuid;
            v_upload_id uuid;
            v_existing uuid;
            v_job_id uuid;
            v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            v_org_text := pg_catalog.current_setting('app.current_org_id', true);
            v_user_text := pg_catalog.current_setting('app.current_user_id', true);
            v_principal_type := pg_catalog.current_setting(
                'app.current_principal_type', true
            );
            v_role := pg_catalog.current_setting('app.current_role', true);
            v_gym := pg_catalog.current_setting('app.current_gym_id', true);
            IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
               OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
               OR v_principal_type IS DISTINCT FROM 'owner'
               OR v_role IS DISTINCT FROM 'owner'
               OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '')
               OR p_asset_type NOT IN ('logo', 'cover') THEN
                RAISE EXCEPTION 'organization asset owner context is required'
                    USING ERRCODE = '42501';
            END IF;
            BEGIN
                v_org_id := v_org_text::uuid;
                v_owner_id := v_user_text::uuid;
                v_upload_id := p_upload_id::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'invalid organization asset identifier'
                    USING ERRCODE = '42501';
            END;
            IF p_asset_type = 'cover'
               AND (p_focal_y IS NULL OR p_focal_y < 0 OR p_focal_y > 1) THEN
                RAISE EXCEPTION 'invalid cover focal point' USING ERRCODE = '22023';
            END IF;
            IF p_asset_type = 'logo' AND p_focal_y IS NOT NULL THEN
                RAISE EXCEPTION 'logo focal point is not supported' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_owner_id AND owner_row.org_id = v_org_id
            ) THEN
                RAISE EXCEPTION 'current owner membership is not authoritative'
                    USING ERRCODE = '42501';
            END IF;

            SELECT job.id INTO v_existing
            FROM public.organization_asset_jobs AS job
            WHERE job.organization_id = v_org_id
              AND job.asset_type = p_asset_type
              AND job.upload_id = v_upload_id;
            IF v_existing IS NOT NULL THEN RETURN v_existing; END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(v_org_id::text || ':' || p_asset_type, 0)
            );
            SELECT job.id INTO v_existing
            FROM public.organization_asset_jobs AS job
            WHERE job.organization_id = v_org_id
              AND job.asset_type = p_asset_type
              AND job.upload_id = v_upload_id;
            IF v_existing IS NOT NULL THEN RETURN v_existing; END IF;

            UPDATE public.organization_asset_jobs AS old_job
            SET status = 'superseded', lease_token = NULL, lease_expires_at = NULL,
                failure_code = 'superseded_by_newer_upload', updated_at = v_now
            WHERE old_job.organization_id = v_org_id
              AND old_job.asset_type = p_asset_type
              AND old_job.status IN ('pending', 'processing');

            v_job_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.organization_asset_jobs (
                id, organization_id, requested_by_owner_id, asset_type,
                upload_id, focal_y, request_ip, status, attempt_count,
                last_dispatched_at, created_at, updated_at
            ) VALUES (
                v_job_id, v_org_id, v_owner_id, p_asset_type, v_upload_id,
                CASE WHEN p_asset_type = 'cover' THEN p_focal_y ELSE NULL END,
                NULLIF(pg_catalog.btrim(p_request_ip), ''),
                'pending', 0, v_now, v_now, v_now
            );
            IF p_asset_type = 'logo' THEN
                UPDATE public.organizations
                SET logo_status = 'pending', updated_at = v_now
                WHERE id = v_org_id;
            ELSE
                UPDATE public.organizations
                SET cover_status = 'pending', updated_at = v_now
                WHERE id = v_org_id;
            END IF;
            RETURN v_job_id;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.claim_organization_asset_job(
            p_job_id uuid,
            p_lease_token uuid,
            p_lease_seconds integer
        ) RETURNS TABLE (
            organization_id uuid, asset_type text, upload_id text,
            focal_y numeric, request_ip text, requested_by_owner_id uuid,
            attempt_count integer
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF p_lease_token IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 900 THEN
                RAISE EXCEPTION 'invalid organization asset lease' USING ERRCODE = '42501';
            END IF;
            SELECT job.* INTO v_job
            FROM public.organization_asset_jobs AS job
            WHERE job.id = p_job_id FOR UPDATE;
            IF NOT FOUND OR v_job.status NOT IN ('pending', 'processing') THEN RETURN; END IF;
            IF v_job.status = 'processing'
               AND v_job.lease_expires_at IS NOT NULL
               AND v_job.lease_expires_at > v_now THEN
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_job.requested_by_owner_id
                  AND owner_row.org_id = v_job.organization_id
            ) THEN
                UPDATE public.organization_asset_jobs
                SET status = 'cancelled', failure_code = 'owner_membership_revoked',
                    lease_token = NULL, lease_expires_at = NULL, updated_at = v_now
                WHERE id = p_job_id;
                RETURN;
            END IF;
            UPDATE public.organization_asset_jobs
            SET status = 'processing', lease_token = p_lease_token,
                lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
                attempt_count = attempt_count + 1, failure_code = NULL, updated_at = v_now
            WHERE id = p_job_id;
            IF v_job.asset_type = 'logo' THEN
                UPDATE public.organizations SET logo_status = 'processing', updated_at = v_now
                WHERE id = v_job.organization_id;
            ELSE
                UPDATE public.organizations SET cover_status = 'processing', updated_at = v_now
                WHERE id = v_job.organization_id;
            END IF;
            organization_id := v_job.organization_id;
            asset_type := v_job.asset_type;
            upload_id := pg_catalog.replace(v_job.upload_id::text, '-', '');
            focal_y := v_job.focal_y;
            request_ip := v_job.request_ip;
            requested_by_owner_id := v_job.requested_by_owner_id;
            attempt_count := v_job.attempt_count + 1;
            RETURN NEXT;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.finalize_organization_asset_job(
            p_job_id uuid,
            p_lease_token uuid,
            p_width integer,
            p_height integer,
            p_size_bytes bigint,
            p_content_type text
        ) RETURNS TABLE (applied boolean, old_keys text[])
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_upload_hex text;
            v_original text;
            v_small text;
            v_medium text;
            v_large text;
            v_old_keys text[];
            v_old_primary text;
        BEGIN
            IF p_lease_token IS NULL OR p_width < 1 OR p_height < 1
               OR p_size_bytes < 1
               OR p_content_type NOT IN ('image/png', 'image/jpeg', 'image/webp') THEN
                RAISE EXCEPTION 'invalid organization asset finalization'
                    USING ERRCODE = '22023';
            END IF;
            SELECT job.* INTO v_job
            FROM public.organization_asset_jobs AS job
            WHERE job.id = p_job_id FOR UPDATE;
            IF NOT FOUND OR v_job.status <> 'processing'
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= v_now THEN
                applied := false; old_keys := ARRAY[]::text[]; RETURN NEXT; RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_job.requested_by_owner_id
                  AND owner_row.org_id = v_job.organization_id
            ) THEN
                UPDATE public.organization_asset_jobs
                SET status = 'cancelled', failure_code = 'owner_membership_revoked',
                    lease_token = NULL, lease_expires_at = NULL, updated_at = v_now
                WHERE id = p_job_id;
                applied := false; old_keys := ARRAY[]::text[]; RETURN NEXT; RETURN;
            END IF;

            v_upload_hex := pg_catalog.replace(v_job.upload_id::text, '-', '');
            v_original := 'originals/' || v_job.organization_id::text || '/' ||
                          v_upload_hex || '_original';
            IF v_job.asset_type = 'logo' THEN
                v_small := 'logos/' || v_job.organization_id::text || '/' || v_upload_hex || '_thumb.webp';
                v_medium := 'logos/' || v_job.organization_id::text || '/' || v_upload_hex || '_medium.webp';
                v_large := 'logos/' || v_job.organization_id::text || '/' || v_upload_hex || '_full.webp';
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.logo_key, organization.logo_thumb_key,
                    organization.logo_medium_key, organization.logo_full_key
                ]::text[], NULL), organization.logo_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_job.organization_id;
                UPDATE public.organizations
                SET logo_key = v_original, logo_thumb_key = v_small,
                    logo_medium_key = v_medium, logo_full_key = v_large,
                    logo_status = 'ready',
                    logo_meta = pg_catalog.jsonb_build_object(
                        'width', p_width, 'height', p_height,
                        'size_bytes', p_size_bytes, 'mime_type', p_content_type
                    ),
                    logo_updated_at = v_now,
                    logo_updated_by = v_job.requested_by_owner_id,
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            ELSE
                v_small := 'covers/' || v_job.organization_id::text || '/' || v_upload_hex || '_mobile.webp';
                v_medium := 'covers/' || v_job.organization_id::text || '/' || v_upload_hex || '_tablet.webp';
                v_large := 'covers/' || v_job.organization_id::text || '/' || v_upload_hex || '_desktop.webp';
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.cover_key, organization.cover_mobile_key,
                    organization.cover_tablet_key, organization.cover_desktop_key
                ]::text[], NULL), organization.cover_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_job.organization_id;
                UPDATE public.organizations
                SET cover_key = v_original, cover_mobile_key = v_small,
                    cover_tablet_key = v_medium, cover_desktop_key = v_large,
                    cover_status = 'ready',
                    cover_meta = pg_catalog.jsonb_build_object(
                        'width', p_width, 'height', p_height,
                        'size_bytes', p_size_bytes, 'mime_type', p_content_type,
                        'focal_point_y', v_job.focal_y
                    ),
                    cover_updated_at = v_now,
                    cover_updated_by = v_job.requested_by_owner_id,
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            END IF;
            INSERT INTO public.organization_asset_audit (
                id, org_id, changed_by, asset_type, old_s3_key,
                new_s3_key, action, action_detail, ip_address
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_job.organization_id,
                v_job.requested_by_owner_id, v_job.asset_type,
                v_old_primary, v_original, 'uploaded',
                pg_catalog.jsonb_build_object(
                    'source', 'p3e_fenced_worker', 'job_id', p_job_id,
                    'attempt_count', v_job.attempt_count
                ), v_job.request_ip
            );
            UPDATE public.organization_asset_jobs
            SET status = 'completed', lease_token = NULL, lease_expires_at = NULL,
                failure_code = NULL, completed_at = v_now, updated_at = v_now
            WHERE id = p_job_id;
            applied := true;
            old_keys := COALESCE(v_old_keys, ARRAY[]::text[]);
            RETURN NEXT;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.fail_organization_asset_job(
            p_job_id uuid,
            p_lease_token uuid,
            p_failure_code text
        ) RETURNS text
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_retryable boolean;
            v_next_status text;
        BEGIN
            IF p_lease_token IS NULL OR p_failure_code NOT IN (
                's3_fetch_error', 'malware_detected', 'antivirus_unavailable',
                'validation_failed', 'processing_failed'
            ) THEN
                RAISE EXCEPTION 'invalid organization asset failure command'
                    USING ERRCODE = '42501';
            END IF;
            SELECT job.* INTO v_job
            FROM public.organization_asset_jobs AS job
            WHERE job.id = p_job_id FOR UPDATE;
            IF NOT FOUND OR v_job.status <> 'processing'
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= v_now THEN
                RETURN NULL;
            END IF;
            v_retryable := p_failure_code IN (
                's3_fetch_error', 'antivirus_unavailable', 'processing_failed'
            ) AND v_job.attempt_count < 5;
            v_next_status := CASE WHEN v_retryable THEN 'pending' ELSE 'failed' END;
            UPDATE public.organization_asset_jobs
            SET status = v_next_status, lease_token = NULL, lease_expires_at = NULL,
                failure_code = p_failure_code,
                completed_at = CASE WHEN v_retryable THEN NULL ELSE v_now END,
                updated_at = v_now
            WHERE id = p_job_id;
            IF v_job.asset_type = 'logo' THEN
                UPDATE public.organizations
                SET logo_status = CASE WHEN v_retryable
                        THEN 'pending'::public.asset_status_enum
                        ELSE 'failed'::public.asset_status_enum END,
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            ELSE
                UPDATE public.organizations
                SET cover_status = CASE WHEN v_retryable
                        THEN 'pending'::public.asset_status_enum
                        ELSE 'failed'::public.asset_status_enum END,
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            END IF;
            IF NOT v_retryable THEN
                INSERT INTO public.organization_asset_audit (
                    id, org_id, changed_by, asset_type, action,
                    action_detail, ip_address
                ) VALUES (
                    pg_catalog.gen_random_uuid(), v_job.organization_id,
                    v_job.requested_by_owner_id, v_job.asset_type, 'failed',
                    pg_catalog.jsonb_build_object(
                        'source', 'p3e_fenced_worker', 'job_id', p_job_id,
                        'reason', p_failure_code,
                        'attempt_count', v_job.attempt_count
                    ), v_job.request_ip
                );
            END IF;
            RETURN v_next_status;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.dispatchable_organization_asset_jobs(
            p_batch_size integer
        ) RETURNS TABLE (job_id uuid, asset_type text)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE v_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true)
                   IS DISTINCT FROM 'platform'
               OR p_batch_size < 1 OR p_batch_size > 100 THEN
                RAISE EXCEPTION 'invalid organization asset dispatch command'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT job.id
                FROM public.organization_asset_jobs AS job
                WHERE (
                    job.status = 'pending'
                    AND (job.last_dispatched_at IS NULL
                         OR job.last_dispatched_at <= v_now - interval '30 seconds')
                ) OR (
                    job.status = 'processing' AND job.lease_expires_at <= v_now
                    AND (job.last_dispatched_at IS NULL
                         OR job.last_dispatched_at <= v_now - interval '30 seconds')
                )
                ORDER BY job.created_at, job.id
                LIMIT p_batch_size FOR UPDATE SKIP LOCKED
            ), updated AS (
                UPDATE public.organization_asset_jobs AS job
                SET last_dispatched_at = v_now, updated_at = v_now
                FROM candidates WHERE job.id = candidates.id
                RETURNING job.id, job.asset_type
            )
            SELECT updated.id, updated.asset_type FROM updated ORDER BY updated.id;
        END;
        $function$;
    """)

    for signature in (
        _ENQUEUE_SIGNATURE, _CLAIM_SIGNATURE, _FINALIZE_SIGNATURE,
        _FAIL_SIGNATURE, _DISPATCH_SIGNATURE,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENQUEUE_SIGNATURE} TO {_API_ROLE}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_CLAIM_SIGNATURE}, {_FINALIZE_SIGNATURE}, "
        f"{_FAIL_SIGNATURE} TO {_WORKER_ROLE}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_DISPATCH_SIGNATURE} TO {_MAINTENANCE_ROLE}"
    )
    op.execute("RESET ROLE")
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in (
        _DISPATCH_SIGNATURE, _FAIL_SIGNATURE, _FINALIZE_SIGNATURE,
        _CLAIM_SIGNATURE, _ENQUEUE_SIGNATURE,
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("RESET ROLE")
    op.execute("""
        REVOKE INSERT (
            id, org_id, changed_by, asset_type, old_s3_key, new_s3_key,
            action, action_detail, ip_address
        ) ON TABLE public.organization_asset_audit FROM app_security_owner
    """)
    op.execute("""
        REVOKE UPDATE (
            logo_key, logo_thumb_key, logo_medium_key, logo_full_key,
            logo_meta, logo_status, logo_updated_at, logo_updated_by,
            cover_key, cover_mobile_key, cover_tablet_key, cover_desktop_key,
            cover_meta, cover_status, cover_updated_at, cover_updated_by
        ), SELECT (
            logo_key, logo_meta, logo_updated_at, logo_updated_by,
            cover_key, cover_meta, cover_updated_at, cover_updated_by
        ) ON TABLE public.organizations FROM app_security_owner
    """)
    op.execute("DROP TABLE public.organization_asset_jobs")
    _require_predecessor(bind)
