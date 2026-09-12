"""P3E: cancel fenced asset jobs during authorized branding deletion.

Revision ID: o07d8e9f0a2f
Revises: n07d8e9f0a2e
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "o07d8e9f0a2f"
down_revision = "n07d8e9f0a2e"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API_ROLE = "app_runtime"
_SIGNATURE = "app_secure.delete_current_organization_asset(text)"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E asset delete migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _function_row(bind):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   'app_runtime', procedure.oid, 'EXECUTE'
               ) AS api_execute,
               pg_catalog.has_function_privilege(
                   'worker_runtime', procedure.oid, 'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   'auth_runtime', procedure.oid, 'EXECUTE'
               ) AS auth_execute,
               pg_catalog.has_function_privilege(
                   'lifecycle_maintenance_runtime', procedure.oid, 'EXECUTE'
               ) AS maintenance_execute,
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
          AND procedure.proname = 'delete_current_organization_asset'
          AND procedure.pronargs = 1
          AND procedure.prokind = 'f'
    """)).mappings().one_or_none()


def _require_predecessor(bind) -> None:
    if _function_row(bind) is not None:
        raise RuntimeError("organization asset delete capability already exists")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.to_regclass('public.organization_asset_jobs') IS NOT NULL"
    )).scalar_one():
        raise RuntimeError("P3E asset job predecessor is missing")


def _require_forward(bind) -> None:
    row = _function_row(bind)
    if row is None:
        raise RuntimeError("organization asset delete capability is missing")
    if (
        row["owner_name"] != _SECURITY_OWNER
        or not bool(row["prosecdef"])
        or row["volatility"] != "v"
        or set(row["proconfig"] or [])
        != {"search_path=pg_catalog", "row_security=on"}
        or not bool(row["api_execute"])
        or bool(row["worker_execute"])
        or bool(row["auth_execute"])
        or bool(row["maintenance_execute"])
        or bool(row["public_execute"])
    ):
        raise RuntimeError("organization asset delete capability contract drift")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.delete_current_organization_asset(
            p_asset_type text
        ) RETURNS text[]
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
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_old_keys text[];
            v_old_primary text;
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
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'invalid organization asset principal context'
                    USING ERRCODE = '42501';
            END;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_owner_id AND owner_row.org_id = v_org_id
            ) THEN
                RAISE EXCEPTION 'current owner membership is not authoritative'
                    USING ERRCODE = '42501';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(v_org_id::text || ':' || p_asset_type, 0)
            );
            UPDATE public.organization_asset_jobs AS job
            SET status = 'cancelled', lease_token = NULL, lease_expires_at = NULL,
                failure_code = 'asset_deleted', updated_at = v_now
            WHERE job.organization_id = v_org_id
              AND job.asset_type = p_asset_type
              AND job.status IN ('pending', 'processing');

            IF p_asset_type = 'logo' THEN
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.logo_key, organization.logo_thumb_key,
                    organization.logo_medium_key, organization.logo_full_key
                ]::text[], NULL), organization.logo_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_org_id;
                UPDATE public.organizations
                SET logo_key = NULL, logo_thumb_key = NULL,
                    logo_medium_key = NULL, logo_full_key = NULL,
                    logo_meta = NULL, logo_status = NULL,
                    logo_updated_at = v_now, logo_updated_by = v_owner_id,
                    updated_at = v_now
                WHERE id = v_org_id;
            ELSE
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.cover_key, organization.cover_mobile_key,
                    organization.cover_tablet_key, organization.cover_desktop_key
                ]::text[], NULL), organization.cover_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_org_id;
                UPDATE public.organizations
                SET cover_key = NULL, cover_mobile_key = NULL,
                    cover_tablet_key = NULL, cover_desktop_key = NULL,
                    cover_meta = NULL, cover_status = NULL,
                    cover_updated_at = v_now, cover_updated_by = v_owner_id,
                    updated_at = v_now
                WHERE id = v_org_id;
            END IF;

            IF v_old_primary IS NOT NULL THEN
                INSERT INTO public.organization_asset_audit (
                    id, org_id, changed_by, asset_type, old_s3_key,
                    action, action_detail
                ) VALUES (
                    pg_catalog.gen_random_uuid(), v_org_id, v_owner_id,
                    p_asset_type, v_old_primary, 'deleted',
                    pg_catalog.jsonb_build_object('source', 'p3e_bounded_delete')
                );
            END IF;
            RETURN COALESCE(v_old_keys, ARRAY[]::text[]);
        END;
        $function$;
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_API_ROLE}")
    op.execute("RESET ROLE")
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute("RESET ROLE")
    _require_predecessor(bind)
