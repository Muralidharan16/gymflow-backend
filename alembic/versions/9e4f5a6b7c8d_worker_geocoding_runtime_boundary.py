"""Bind tenant-safe organization geocoding to worker_runtime.

Revision ID: 9e4f5a6b7c8d
Revises: 8d3e4f5a6b7c
Create Date: 2026-08-13

The worker receives only geocoding columns protected by existing FORCE RLS and
Google Places cache access. member_addresses, notifications and event_outbox
remain unavailable directly. Permanent failure emission is exposed through one
bounded app_security_owner SECURITY DEFINER function.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "9e4f5a6b7c8d"
down_revision = "8d3e4f5a6b7c"
branch_labels = None
depends_on = None


def _require_migration_owner(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname = current_user
    """)).one()
    if row[0] != "migration_owner" or row[1] != "migration_owner":
        raise RuntimeError("9e4f geocoding boundary requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _require_force_rls(bind, relation: str) -> None:
    row = bind.execute(sa.text("""
        SELECT c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = 'public' AND c.relname = :relation
          AND c.relkind IN ('r','p')
    """), {"relation": relation}).one_or_none()
    if row is None or not row[0] or not row[1]:
        raise RuntimeError(f"public.{relation} must retain ENABLE + FORCE RLS")


def _has_table(bind, role: str, relation: str, privilege: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT pg_catalog.has_table_privilege(:role, :relation, :privilege)"
    ), {
        "role": role,
        "relation": f"public.{relation}",
        "privilege": privilege,
    }).scalar_one())


def _require_no_direct_sensitive_worker_access(bind) -> None:
    for relation in ("member_addresses", "notifications", "event_outbox"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if _has_table(bind, "worker_runtime", relation, privilege):
                raise RuntimeError(
                    f"worker_runtime leaked direct {privilege} on public.{relation}"
                )


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_force_rls(bind, "organization_addresses")
    _require_force_rls(bind, "branch_geolocation_state")
    _require_no_direct_sensitive_worker_access(bind)

    op.execute("""
        GRANT SELECT (
            id, org_id, address_line1, city, state_province, postal_code,
            country_code, formatted_address, google_place_id, deleted_at
        ) ON TABLE public.organization_addresses TO worker_runtime
    """)
    op.execute("""
        GRANT UPDATE (formatted_address)
        ON TABLE public.organization_addresses TO worker_runtime
    """)
    op.execute("""
        GRANT SELECT (
            address_id, org_id, coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state TO worker_runtime
    """)
    op.execute("""
        GRANT INSERT (
            address_id, org_id, coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state TO worker_runtime
    """)
    op.execute("""
        GRANT UPDATE (
            coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state TO worker_runtime
    """)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.google_places_cache TO worker_runtime"
    )

    op.execute(
        "GRANT SELECT (id, org_id, deleted_at) ON TABLE "
        "public.organization_addresses TO app_security_owner"
    )
    op.execute("GRANT INSERT ON TABLE public.notifications TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE public.event_outbox TO app_security_owner")
    op.execute("GRANT USAGE ON SCHEMA app_secure TO worker_runtime")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("""
        CREATE FUNCTION app_secure.record_org_geocoding_failure(
            p_address_id uuid,
            p_org_id uuid,
            p_error text,
            p_retry_count integer,
            p_message text,
            p_lineage_id uuid
        ) RETURNS void
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_org uuid := NULLIF(
                pg_catalog.current_setting('app.current_org_id', true), ''
            )::uuid;
        BEGIN
            IF p_address_id IS NULL OR p_org_id IS NULL
               OR p_retry_count < 1 OR p_message IS NULL OR p_lineage_id IS NULL
               OR v_org IS NULL OR p_org_id IS DISTINCT FROM v_org THEN
                RAISE EXCEPTION 'invalid tenant-bound geocoding failure command'
                    USING ERRCODE = '42501';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM public.organization_addresses
                WHERE id = p_address_id AND org_id = p_org_id AND deleted_at IS NULL
            ) THEN
                RAISE EXCEPTION 'geocoding address is not visible in tenant context'
                    USING ERRCODE = '42501';
            END IF;

            INSERT INTO public.notifications (id, org_id, message, is_read)
            VALUES (pg_catalog.gen_random_uuid(), p_org_id, p_message, false);

            INSERT INTO public.event_outbox (
                event_id, event_type, payload, tenant_id, lineage_id
            ) VALUES (
                pg_catalog.gen_random_uuid(),
                'maps.verification.failed',
                pg_catalog.jsonb_build_object(
                    'address_id', p_address_id,
                    'org_id', p_org_id,
                    'error', p_error,
                    'retry_count', p_retry_count
                ),
                p_org_id,
                p_lineage_id
            ) ON CONFLICT DO NOTHING;
        END;
        $function$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        app_secure.record_org_geocoding_failure(uuid,uuid,text,integer,text,uuid)
        FROM PUBLIC
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION
        app_secure.record_org_geocoding_failure(uuid,uuid,text,integer,text,uuid)
        TO worker_runtime
    """)
    op.execute("RESET ROLE")

    _require_no_direct_sensitive_worker_access(bind)
    if not bind.execute(sa.text("""
        SELECT pg_catalog.has_function_privilege(
            'worker_runtime',
            'app_secure.record_org_geocoding_failure(uuid,uuid,text,integer,text,uuid)',
            'EXECUTE'
        )
    """)).scalar_one():
        raise RuntimeError("worker lacks bounded geocoding failure function")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _require_force_rls(bind, "organization_addresses")
    _require_force_rls(bind, "branch_geolocation_state")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("""
        DROP FUNCTION
        app_secure.record_org_geocoding_failure(uuid,uuid,text,integer,text,uuid)
    """)
    op.execute("RESET ROLE")

    op.execute("REVOKE USAGE ON SCHEMA app_secure FROM worker_runtime")
    op.execute("REVOKE INSERT ON TABLE public.event_outbox FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE public.notifications FROM app_security_owner")
    op.execute("""
        REVOKE SELECT (id, org_id, deleted_at)
        ON TABLE public.organization_addresses FROM app_security_owner
    """)
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON TABLE public.google_places_cache FROM worker_runtime"
    )
    op.execute("""
        REVOKE UPDATE (
            coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state FROM worker_runtime
    """)
    op.execute("""
        REVOKE INSERT (
            address_id, org_id, coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state FROM worker_runtime
    """)
    op.execute("""
        REVOKE SELECT (
            address_id, org_id, coordinates, validation_status, geocode_attempts,
            last_geocode_attempt_at, next_retry_at, geocoded_at, geocode_provider
        ) ON TABLE public.branch_geolocation_state FROM worker_runtime
    """)
    op.execute("""
        REVOKE UPDATE (formatted_address)
        ON TABLE public.organization_addresses FROM worker_runtime
    """)
    op.execute("""
        REVOKE SELECT (
            id, org_id, address_line1, city, state_province, postal_code,
            country_code, formatted_address, google_place_id, deleted_at
        ) ON TABLE public.organization_addresses FROM worker_runtime
    """)

    _require_no_direct_sensitive_worker_access(bind)
