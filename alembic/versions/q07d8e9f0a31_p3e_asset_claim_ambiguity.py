"""P3E: disambiguate the fenced asset claim attempt counter.

Revision ID: q07d8e9f0a31
Revises: p07d8e9f0a30
Create Date: 2026-08-16

The claim capability returns an OUT column named ``attempt_count``. PostgreSQL
therefore treats an unqualified ``attempt_count`` reference inside PL/pgSQL as
ambiguous between the OUT variable and the durable job column. Replace only the
function body so the increment is explicitly bound to the locked job row. No
ACL, ownership, RLS, or role graph change is introduced.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "q07d8e9f0a31"
down_revision = "p07d8e9f0a30"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_CLAIM_SIGNATURE = "app_secure.claim_organization_asset_job(uuid,uuid,integer)"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E asset claim correction requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(session_user, :role, 'SET')"),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _claim_contract(bind) -> dict[str, object]:
    row = bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   'worker_runtime', procedure.oid, 'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   'app_runtime', procedure.oid, 'EXECUTE'
               ) AS api_execute,
               pg_catalog.has_function_privilege(
                   'auth_runtime', procedure.oid, 'EXECUTE'
               ) AS auth_execute,
               pg_catalog.has_function_privilege(
                   'lifecycle_maintenance_runtime', procedure.oid, 'EXECUTE'
               ) AS maintenance_execute,
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
               ) AS public_execute,
               pg_catalog.pg_get_functiondef(procedure.oid) AS definition
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = 'claim_organization_asset_job'
          AND pg_catalog.pg_get_function_identity_arguments(procedure.oid)
              = 'p_job_id uuid, p_lease_token uuid, p_lease_seconds integer'
    """)).mappings().one_or_none()
    if row is None:
        raise RuntimeError("P3E asset claim capability is missing")
    if (
        row["owner_name"] != _SECURITY_OWNER
        or not bool(row["prosecdef"])
        or row["volatility"] != "v"
        or set(row["proconfig"] or [])
        != {"search_path=pg_catalog", "row_security=on"}
        or not bool(row["worker_execute"])
        or bool(row["api_execute"])
        or bool(row["auth_execute"])
        or bool(row["maintenance_execute"])
        or bool(row["public_execute"])
    ):
        raise RuntimeError("P3E asset claim capability ACL/owner contract drifted")
    return dict(row)


def _replace_claim(*, qualified_increment: bool) -> None:
    increment = (
        "attempt_count = job.attempt_count + 1"
        if qualified_increment
        else "attempt_count = attempt_count + 1"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(sa.text(r"""
        CREATE OR REPLACE FUNCTION app_secure.claim_organization_asset_job(
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
                UPDATE public.organization_asset_jobs AS job
                SET status = 'cancelled', failure_code = 'owner_membership_revoked',
                    lease_token = NULL, lease_expires_at = NULL, updated_at = v_now
                WHERE job.id = p_job_id;
                RETURN;
            END IF;
            UPDATE public.organization_asset_jobs AS job
            SET status = 'processing', lease_token = p_lease_token,
                lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
                __INCREMENT__, failure_code = NULL, updated_at = v_now
            WHERE job.id = p_job_id;
            IF v_job.asset_type = 'logo' THEN
                UPDATE public.organizations
                SET logo_status = 'processing', updated_at = v_now
                WHERE id = v_job.organization_id;
            ELSE
                UPDATE public.organizations
                SET cover_status = 'processing', updated_at = v_now
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
    """.replace("__INCREMENT__", increment)))
    op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    before = _claim_contract(bind)
    if "attempt_count = attempt_count + 1" not in str(before["definition"]):
        raise RuntimeError("P3E asset claim predecessor body drifted")
    _replace_claim(qualified_increment=True)
    after = _claim_contract(bind)
    definition = str(after["definition"])
    if "attempt_count = job.attempt_count + 1" not in definition:
        raise RuntimeError("P3E asset claim correction was not installed")
    if "attempt_count = attempt_count + 1" in definition:
        raise RuntimeError("ambiguous P3E asset claim increment survived correction")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    before = _claim_contract(bind)
    if "attempt_count = job.attempt_count + 1" not in str(before["definition"]):
        raise RuntimeError("P3E asset claim corrected body drifted")
    _replace_claim(qualified_increment=False)
    after = _claim_contract(bind)
    if "attempt_count = attempt_count + 1" not in str(after["definition"]):
        raise RuntimeError("P3E asset claim predecessor body was not restored")
