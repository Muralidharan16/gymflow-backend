"""Move database-global platform maintenance behind bounded app_secure functions.

Revision ID: af5b6c7d8e9f
Revises: 9e4f5a6b7c8d
Create Date: 2026-08-14

The FastAPI/API identity must never receive cross-tenant maintenance privileges.
This revision exposes only four bounded SECURITY DEFINER functions to the
isolated maintenance capability.  The functions require transaction-local
``app.internal_maintenance=platform`` and operate in bounded batches.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "af5b6c7d8e9f"
down_revision = "9e4f5a6b7c8d"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_POLICY = "platform_maintenance_geolocation"


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("platform maintenance migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(sa.text("""
        SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname IN (:security_owner, :maintenance_role)
    """), {
        "security_owner": _SECURITY_OWNER,
        "maintenance_role": _MAINTENANCE_ROLE,
    }).mappings().all()
    by_name = {row["rolname"]: row for row in roles}
    if set(by_name) != {_SECURITY_OWNER, _MAINTENANCE_ROLE}:
        raise RuntimeError("required platform-maintenance roles are missing")
    for name, role in by_name.items():
        if role["rolcanlogin"] or role["rolsuper"] or role["rolinherit"] or role["rolbypassrls"]:
            raise RuntimeError(f"managed role {name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS")

    edge = bind.execute(sa.text("""
        SELECT m.admin_option, m.inherit_option, m.set_option
        FROM pg_catalog.pg_auth_members AS m
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = m.member
        WHERE granted.rolname = :granted AND member_role.rolname = :member
    """), {
        "granted": _SECURITY_OWNER,
        "member": _MIGRATION_OWNER,
    }).mappings().all()
    if len(edge) != 1 or edge[0]["admin_option"] or edge[0]["inherit_option"] or not edge[0]["set_option"]:
        raise RuntimeError("migration_owner -> app_security_owner must remain SET-only")


def _require_force_rls(bind, relation: str) -> None:
    row = bind.execute(sa.text("""
        SELECT c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = 'public' AND c.relname = :relation
          AND c.relkind IN ('r', 'p')
    """), {"relation": relation}).one_or_none()
    if row is None or not row[0] or not row[1]:
        raise RuntimeError(f"public.{relation} must retain ENABLE + FORCE RLS")


def _direct_schema_usage(bind, role: str) -> bool:
    return bool(_scalar(bind, """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(ns.nspacl, pg_catalog.acldefault('n', ns.nspowner))
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE ns.nspname = 'app_secure'
              AND grantee.rolname = :role
              AND acl.privilege_type = 'USAGE'
        )
    """, {"role": role}))


def _has_table(bind, role: str, relation: str, privilege: str) -> bool:
    return bool(_scalar(
        bind,
        "SELECT pg_catalog.has_table_privilege(:role, :relation, :privilege)",
        {"role": role, "relation": f"public.{relation}", "privilege": privilege},
    ))


def _require_predecessor_acl(bind) -> None:
    if _direct_schema_usage(bind, _MAINTENANCE_ROLE):
        raise RuntimeError(
            "platform-maintenance predecessor unexpectedly grants app_secure USAGE "
            "to lifecycle_maintenance_runtime"
        )
    for relation in (
        "active_idempotency_keys",
        "branch_geolocation_state",
        "google_places_cache",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if _has_table(bind, _MAINTENANCE_ROLE, relation, privilege):
                raise RuntimeError(
                    f"maintenance role unexpectedly has direct {privilege} on public.{relation}"
                )


def _require_forward_contract(bind) -> None:
    _require_force_rls(bind, "branch_geolocation_state")
    if not _direct_schema_usage(bind, _MAINTENANCE_ROLE):
        raise RuntimeError("maintenance role lacks app_secure USAGE")

    for relation in (
        "active_idempotency_keys",
        "branch_geolocation_state",
        "google_places_cache",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if _has_table(bind, _MAINTENANCE_ROLE, relation, privilege):
                raise RuntimeError(
                    f"maintenance role leaked direct {privilege} on public.{relation}"
                )

    policy = bind.execute(sa.text("""
        SELECT p.polcmd::text,
               pg_catalog.pg_get_expr(p.polqual, p.polrelid, true)::text,
               pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid, true)::text,
               ARRAY(
                   SELECT role.rolname::text
                   FROM pg_catalog.unnest(p.polroles) AS role_oid(oid)
                   JOIN pg_catalog.pg_roles AS role ON role.oid = role_oid.oid
                   ORDER BY role.rolname
               ) AS roles
        FROM pg_catalog.pg_policy AS p
        JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = 'public'
          AND c.relname = 'branch_geolocation_state'
          AND p.polname = :policy
    """), {"policy": _POLICY}).mappings().one_or_none()
    if policy is None or policy["polcmd"] != "*" or policy["roles"] != [_SECURITY_OWNER]:
        raise RuntimeError("platform geolocation maintenance RLS policy drifted")
    for expr in (policy["pg_get_expr"], policy["pg_get_expr_1"]):
        if expr is None or "app.internal_maintenance" not in expr or "platform" not in expr:
            raise RuntimeError("platform geolocation maintenance RLS predicate drifted")

    signatures = {
        "reclaim_stale_idempotency_keys": 2,
        "archive_expired_idempotency_keys": 2,
        "claim_due_geocoding_reverification": 1,
        "cleanup_expired_places_cache": 1,
    }
    for function_name, nargs in signatures.items():
        rows = bind.execute(sa.text("""
            SELECT owner.rolname::text AS owner_name,
                   p.prosecdef,
                   p.proconfig,
                   p.prosrc,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(
                           COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                       ) AS acl
                       JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                       WHERE grantee.rolname = :maintenance_role
                         AND acl.privilege_type = 'EXECUTE'
                   ) AS maintenance_execute,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(
                           COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                       ) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = p.pronamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
            WHERE ns.nspname = 'app_secure'
              AND p.proname = :function_name
              AND p.pronargs = :nargs
              AND p.prokind = 'f'
        """), {
            "function_name": function_name,
            "nargs": nargs,
            "maintenance_role": _MAINTENANCE_ROLE,
        }).mappings().all()
        if len(rows) != 1:
            raise RuntimeError(f"platform maintenance function {function_name} is ambiguous")
        row = rows[0]
        if row["owner_name"] != _SECURITY_OWNER or not row["prosecdef"]:
            raise RuntimeError(f"platform maintenance function {function_name} owner/security drifted")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"platform maintenance function {function_name} settings drifted")
        if not row["maintenance_execute"] or row["public_execute"]:
            raise RuntimeError(f"platform maintenance function {function_name} EXECUTE ACL drifted")
        if "app.internal_maintenance" not in (row["prosrc"] or ""):
            raise RuntimeError(f"platform maintenance function {function_name} lost context gate")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_force_rls(bind, "branch_geolocation_state")
    _require_predecessor_acl(bind)

    op.execute("""
        GRANT SELECT (id, status, locked_at, updated_at),
              UPDATE (status, locked_by, locked_at, updated_at),
              DELETE
        ON TABLE public.active_idempotency_keys TO app_security_owner
    """)
    op.execute("""
        GRANT SELECT (
            address_id, org_id, validation_status, geocode_attempts,
            next_retry_at, geocoded_at
        ), UPDATE (validation_status, next_retry_at)
        ON TABLE public.branch_geolocation_state TO app_security_owner
    """)
    op.execute("""
        GRANT SELECT (place_id, expires_at), DELETE
        ON TABLE public.google_places_cache TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("GRANT USAGE ON SCHEMA app_secure TO lifecycle_maintenance_runtime")
    op.execute("""
        CREATE POLICY platform_maintenance_geolocation
        ON public.branch_geolocation_state
        FOR ALL
        TO app_security_owner
        USING (
            pg_catalog.current_setting('app.internal_maintenance', true) = 'platform'
        )
        WITH CHECK (
            pg_catalog.current_setting('app.internal_maintenance', true) = 'platform'
        )
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.reclaim_stale_idempotency_keys(
            p_stale_seconds integer,
            p_batch_size integer
        ) RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_count integer := 0;
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true) <> 'platform'
               OR p_stale_seconds < 30 OR p_stale_seconds > 3600
               OR p_batch_size < 1 OR p_batch_size > 5000 THEN
                RAISE EXCEPTION 'invalid platform idempotency reclaim command'
                    USING ERRCODE = '42501';
            END IF;

            WITH target AS (
                SELECT id
                FROM public.active_idempotency_keys
                WHERE status = 'processing'
                  AND locked_at IS NOT NULL
                  AND locked_at < pg_catalog.clock_timestamp()
                      - pg_catalog.make_interval(secs => p_stale_seconds)
                ORDER BY locked_at, id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE public.active_idempotency_keys AS key_data
            SET status = 'available',
                locked_by = NULL,
                locked_at = NULL,
                updated_at = pg_catalog.clock_timestamp()
            FROM target
            WHERE key_data.id = target.id;

            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.archive_expired_idempotency_keys(
            p_retention_hours integer,
            p_batch_size integer
        ) RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_count integer := 0;
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true) <> 'platform'
               OR p_retention_hours < 24 OR p_retention_hours > 720
               OR p_batch_size < 1 OR p_batch_size > 5000 THEN
                RAISE EXCEPTION 'invalid platform idempotency archive command'
                    USING ERRCODE = '42501';
            END IF;

            WITH target AS (
                SELECT id
                FROM public.active_idempotency_keys
                WHERE status <> 'processing'
                  AND updated_at < pg_catalog.clock_timestamp()
                      - pg_catalog.make_interval(hours => p_retention_hours)
                ORDER BY updated_at, id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.active_idempotency_keys AS key_data
            USING target
            WHERE key_data.id = target.id;

            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.claim_due_geocoding_reverification(
            p_batch_size integer
        ) RETURNS TABLE(address_id uuid, org_id uuid)
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true) <> 'platform'
               OR p_batch_size < 1 OR p_batch_size > 500 THEN
                RAISE EXCEPTION 'invalid platform geocoding claim command'
                    USING ERRCODE = '42501';
            END IF;

            RETURN QUERY
            WITH target AS (
                SELECT state.address_id, state.org_id
                FROM public.branch_geolocation_state AS state
                WHERE state.geocode_attempts < 10
                  AND (
                    (
                        state.validation_status = 'success'
                        AND state.geocoded_at IS NOT NULL
                        AND state.geocoded_at < pg_catalog.clock_timestamp()
                            - pg_catalog.make_interval(days => 30)
                    )
                    OR (
                        state.validation_status IN ('pending', 'failed', 'queued')
                        AND (
                            state.next_retry_at IS NULL
                            OR state.next_retry_at <= pg_catalog.clock_timestamp()
                        )
                    )
                  )
                ORDER BY
                    state.next_retry_at NULLS FIRST,
                    state.geocoded_at NULLS FIRST,
                    state.address_id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            ), claimed AS (
                UPDATE public.branch_geolocation_state AS state
                SET validation_status = 'queued',
                    next_retry_at = pg_catalog.clock_timestamp()
                        + pg_catalog.make_interval(mins => 10)
                FROM target
                WHERE state.address_id = target.address_id
                RETURNING state.address_id, state.org_id
            )
            SELECT claimed.address_id, claimed.org_id
            FROM claimed
            ORDER BY claimed.address_id;
        END;
        $function$;
    """)

    op.execute(r"""
        CREATE FUNCTION app_secure.cleanup_expired_places_cache(
            p_batch_size integer
        ) RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_count integer := 0;
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true) <> 'platform'
               OR p_batch_size < 1 OR p_batch_size > 5000 THEN
                RAISE EXCEPTION 'invalid platform places-cache cleanup command'
                    USING ERRCODE = '42501';
            END IF;

            WITH target AS (
                SELECT place_id
                FROM public.google_places_cache
                WHERE expires_at < pg_catalog.clock_timestamp()
                    - pg_catalog.make_interval(days => 90)
                ORDER BY expires_at, place_id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.google_places_cache AS cache
            USING target
            WHERE cache.place_id = target.place_id;

            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$;
    """)

    for signature in (
        "app_secure.reclaim_stale_idempotency_keys(integer,integer)",
        "app_secure.archive_expired_idempotency_keys(integer,integer)",
        "app_secure.claim_due_geocoding_reverification(integer)",
        "app_secure.cleanup_expired_places_cache(integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {signature} TO lifecycle_maintenance_runtime"
        )
    op.execute("RESET ROLE")

    _require_forward_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward_contract(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in (
        "app_secure.cleanup_expired_places_cache(integer)",
        "app_secure.claim_due_geocoding_reverification(integer)",
        "app_secure.archive_expired_idempotency_keys(integer,integer)",
        "app_secure.reclaim_stale_idempotency_keys(integer,integer)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP POLICY platform_maintenance_geolocation ON public.branch_geolocation_state")
    op.execute("REVOKE USAGE ON SCHEMA app_secure FROM lifecycle_maintenance_runtime")
    op.execute("RESET ROLE")

    op.execute("""
        REVOKE SELECT (place_id, expires_at), DELETE
        ON TABLE public.google_places_cache FROM app_security_owner
    """)
    op.execute("""
        REVOKE SELECT (
            address_id, org_id, validation_status, geocode_attempts,
            next_retry_at, geocoded_at
        ), UPDATE (validation_status, next_retry_at)
        ON TABLE public.branch_geolocation_state FROM app_security_owner
    """)
    op.execute("""
        REVOKE SELECT (id, status, locked_at, updated_at),
               UPDATE (status, locked_by, locked_at, updated_at),
               DELETE
        ON TABLE public.active_idempotency_keys FROM app_security_owner
    """)

    _require_force_rls(bind, "branch_geolocation_state")
    _require_predecessor_acl(bind)
