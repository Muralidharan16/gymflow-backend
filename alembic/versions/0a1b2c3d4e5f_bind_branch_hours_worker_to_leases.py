"""Bind branch-hours worker source access and child enqueue to live leases.

Revision ID: 0a1b2c3d4e5f
Revises: 091a2b3c4d5e
Create Date: 2026-08-11

The dedicated worker role is intentionally an internal cross-tenant service
identity, but each processing transaction should still fail closed when its
persisted tenant/branch context does not match the event it actually leased.
This revision therefore:

* replaces GUC-only source policies with live-lease-bound worker policies;
* removes worker direct queue INSERT and replaces broad table UPDATE with exact
  mutable queue columns;
* exposes child-event creation only through a fixed SECURITY DEFINER function
  that validates the parent is currently leased and branch/tenant lineage is
  consistent; and
* keeps DELETE/TRUNCATE/DDL/BYPASSRLS unavailable.

The worker UUID is an operational lease identifier, not a credential. The
security boundary remains the separate worker database login and its bounded
non-PII relation set; lease binding protects transaction correctness and limits
accidental/stale tenant context.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0a1b2c3d4e5f"
down_revision = "091a2b3c4d5e"
branch_labels = None
depends_on = None

_WORKER = "worker_runtime"
_SECURITY_OWNER = "app_security_owner"
_QUEUE = "public.transactional_outbox"
_CHILD_FUNCTION = (
    "public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text)"
)
_MAINTENANCE_TOKEN = "branch_hours_projection"

_SOURCE_POLICIES = {
    "public.org_branches": "branch_hours_worker_branch_read",
    "public.org_branch_state": "branch_hours_worker_branch_state_read",
    "public.organization_operating_hours": "branch_hours_worker_org_hours_read",
    "public.branch_operating_hours": "branch_hours_worker_branch_hours_read",
    "public.branch_special_hours": "branch_hours_worker_special_hours_read",
    "public.branch_hours_projection": (
        "branch_hours_worker_projection_read",
        "branch_hours_worker_projection_insert",
        "branch_hours_worker_projection_update",
    ),
}

_QUEUE_UPDATE_COLUMNS = (
    "leased_by",
    "leased_until",
    "delivery_attempts",
    "last_error",
    "processed_at",
    "dead_lettered_at",
    "available_at",
)
_SECURITY_QUEUE_SELECT_COLUMNS = (
    "id",
    "tenant_id",
    "branch_id",
    "event_type",
    "dedupe_key",
    "correlation_id",
    "leased_by",
    "leased_until",
    "processed_at",
    "dead_lettered_at",
)


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != "migration_owner" or row["current_name"] != "migration_owner":
        raise RuntimeError("0a1b worker lease migration requires migration_owner")
    if any(
        bool(row[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                "SELECT polname::text FROM pg_catalog.pg_policy "
                "WHERE polrelid = CAST(:relation AS regclass)"
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _current_uuid_guc(name: str) -> str:
    return f"""
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('{name}', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('{name}', true), '') AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _maintenance_expr() -> str:
    return (
        "NULLIF(pg_catalog.current_setting('app.internal_maintenance', true), '') "
        f"= '{_MAINTENANCE_TOKEN}'"
    )


def _live_lease_expr(target_org: str, target_branch: str | None) -> str:
    worker_id = _current_uuid_guc("app.worker_id")
    branch_clause = "TRUE"
    if target_branch is not None:
        branch_clause = f"""
            (
                lease_data.event_type = 'branch_hours.organization_changed'
                OR lease_data.branch_id = {target_branch}
            )
        """
    return f"""
        EXISTS (
            SELECT 1
            FROM public.transactional_outbox AS lease_data
            WHERE lease_data.tenant_id = {target_org}
              AND lease_data.leased_by = {worker_id}
              AND lease_data.leased_until > pg_catalog.clock_timestamp()
              AND lease_data.processed_at IS NULL
              AND lease_data.dead_lettered_at IS NULL
              AND {branch_clause}
        )
    """


def _drop_source_policies() -> None:
    for relation, names in _SOURCE_POLICIES.items():
        iterable = (names,) if isinstance(names, str) else names
        for name in iterable:
            op.execute(f"DROP POLICY {name} ON {relation}")


def _create_lease_bound_source_policies() -> None:
    current_org = _current_uuid_guc("app.current_org_id")
    maintenance = _maintenance_expr()

    branch_scope = f"""
        org_id = {current_org}
        AND {maintenance}
        AND {_live_lease_expr('org_id', 'id')}
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_read
        ON public.org_branches
        FOR SELECT TO worker_runtime
        USING ({branch_scope})
        """
    )

    state_scope = f"""
        org_id = {current_org}
        AND {maintenance}
        AND {_live_lease_expr('org_id', 'branch_id')}
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_state_read
        ON public.org_branch_state
        FOR SELECT TO worker_runtime
        USING ({state_scope})
        """
    )

    org_hours_scope = f"""
        org_id = {current_org}
        AND {maintenance}
        AND {_live_lease_expr('org_id', None)}
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_org_hours_read
        ON public.organization_operating_hours
        FOR SELECT TO worker_runtime
        USING ({org_hours_scope})
        """
    )

    for relation, policy_name in (
        ("public.branch_operating_hours", "branch_hours_worker_branch_hours_read"),
        ("public.branch_special_hours", "branch_hours_worker_special_hours_read"),
    ):
        table_name = relation.split(".", 1)[1]
        target_branch = f"{table_name}.branch_id"
        scope = f"""
            {maintenance}
            AND EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = {target_branch}
                  AND branch_data.org_id = {current_org}
                  AND {_live_lease_expr('branch_data.org_id', 'branch_data.id')}
            )
        """
        op.execute(
            f"""
            CREATE POLICY {policy_name}
            ON {relation}
            FOR SELECT TO worker_runtime
            USING ({scope})
            """
        )

    projection_scope = f"""
        {maintenance}
        AND EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            WHERE branch_data.id = branch_hours_projection.branch_id
              AND branch_data.org_id = {current_org}
              AND {_live_lease_expr('branch_data.org_id', 'branch_data.id')}
        )
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_read
        ON public.branch_hours_projection
        FOR SELECT TO worker_runtime
        USING ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_insert
        ON public.branch_hours_projection
        FOR INSERT TO worker_runtime
        WITH CHECK ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_update
        ON public.branch_hours_projection
        FOR UPDATE TO worker_runtime
        USING ({projection_scope})
        WITH CHECK ({projection_scope})
        """
    )


def _create_e7_source_policies() -> None:
    current_org = _current_uuid_guc("app.current_org_id")
    maintenance = _maintenance_expr()
    op.execute(
        f"CREATE POLICY branch_hours_worker_branch_read ON public.org_branches "
        f"FOR SELECT TO worker_runtime USING (org_id = {current_org} AND {maintenance})"
    )
    op.execute(
        f"CREATE POLICY branch_hours_worker_branch_state_read ON public.org_branch_state "
        f"FOR SELECT TO worker_runtime USING (org_id = {current_org} AND {maintenance})"
    )
    op.execute(
        f"CREATE POLICY branch_hours_worker_org_hours_read ON public.organization_operating_hours "
        f"FOR SELECT TO worker_runtime USING (org_id = {current_org} AND {maintenance})"
    )
    for relation, policy_name in (
        ("public.branch_operating_hours", "branch_hours_worker_branch_hours_read"),
        ("public.branch_special_hours", "branch_hours_worker_special_hours_read"),
    ):
        table_name = relation.split(".", 1)[1]
        op.execute(
            f"""
            CREATE POLICY {policy_name}
            ON {relation}
            FOR SELECT TO worker_runtime
            USING (
                {maintenance}
                AND EXISTS (
                    SELECT 1 FROM public.org_branches AS branch_data
                    WHERE branch_data.id = {table_name}.branch_id
                      AND branch_data.org_id = {current_org}
                )
            )
            """
        )
    projection_scope = f"""
        {maintenance}
        AND EXISTS (
            SELECT 1 FROM public.org_branches AS branch_data
            WHERE branch_data.id = branch_hours_projection.branch_id
              AND branch_data.org_id = {current_org}
        )
    """
    op.execute(
        f"CREATE POLICY branch_hours_worker_projection_read ON public.branch_hours_projection "
        f"FOR SELECT TO worker_runtime USING ({projection_scope})"
    )
    op.execute(
        f"CREATE POLICY branch_hours_worker_projection_insert ON public.branch_hours_projection "
        f"FOR INSERT TO worker_runtime WITH CHECK ({projection_scope})"
    )
    op.execute(
        f"CREATE POLICY branch_hours_worker_projection_update ON public.branch_hours_projection "
        f"FOR UPDATE TO worker_runtime USING ({projection_scope}) WITH CHECK ({projection_scope})"
    )


def _create_child_function() -> None:
    op.execute(
        """
        GRANT SELECT (
            id, tenant_id, branch_id, event_type, dedupe_key, correlation_id,
            leased_by, leased_until, processed_at, dead_lettered_at
        )
        ON TABLE public.transactional_outbox
        TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_internal_outbox_read
        ON public.transactional_outbox
        FOR SELECT TO app_security_owner
        USING (TRUE)
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_internal_child_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO app_security_owner
        WITH CHECK (
            event_type = 'branch_hours.branch_changed'
            AND branch_id IS NOT NULL
            AND parent_event_id IS NOT NULL
            AND event_version = 1
            AND correlation_id IS NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.enqueue_branch_hours_child(
            p_parent_event_id uuid,
            p_branch_id uuid,
            p_worker_id uuid,
            p_available_at timestamptz,
            p_reason text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_tenant_id uuid;
            v_parent_type text;
            v_parent_branch uuid;
            v_correlation_id uuid;
            v_event_id uuid;
            v_dedupe_key text;
        BEGIN
            IF p_parent_event_id IS NULL
               OR p_branch_id IS NULL
               OR p_worker_id IS NULL
               OR p_available_at IS NULL
               OR p_reason NOT IN ('organization_hours_changed', 'temporal_refresh')
            THEN
                RAISE EXCEPTION 'invalid branch-hours child enqueue arguments'
                    USING ERRCODE = '22023';
            END IF;

            SELECT tenant_id, event_type, branch_id, correlation_id
            INTO v_tenant_id, v_parent_type, v_parent_branch, v_correlation_id
            FROM public.transactional_outbox
            WHERE id = p_parent_event_id
              AND leased_by = p_worker_id
              AND leased_until > pg_catalog.clock_timestamp()
              AND processed_at IS NULL
              AND dead_lettered_at IS NULL;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'branch-hours child enqueue requires a live owned parent lease'
                    USING ERRCODE = '42501';
            END IF;
            IF v_parent_type NOT IN (
                'branch_hours.branch_changed',
                'branch_hours.organization_changed'
            ) THEN
                RAISE EXCEPTION 'unsupported branch-hours parent event type'
                    USING ERRCODE = '22023';
            END IF;
            IF v_parent_type = 'branch_hours.branch_changed'
               AND v_parent_branch IS DISTINCT FROM p_branch_id
            THEN
                RAISE EXCEPTION 'branch refresh child must retain parent branch lineage'
                    USING ERRCODE = '42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = p_branch_id
                  AND branch_data.org_id = v_tenant_id
            ) THEN
                RAISE EXCEPTION 'branch-hours child branch/tenant lineage mismatch'
                    USING ERRCODE = '42501';
            END IF;

            IF p_reason = 'organization_hours_changed' THEN
                v_dedupe_key := 'fanout:' || p_parent_event_id::text
                    || ':' || p_branch_id::text;
            ELSE
                v_dedupe_key := 'temporal:' || p_branch_id::text
                    || ':' || EXTRACT(EPOCH FROM p_available_at)::text;
            END IF;

            INSERT INTO public.transactional_outbox (
                tenant_id,
                branch_id,
                event_type,
                payload,
                dedupe_key,
                event_version,
                correlation_id,
                available_at,
                parent_event_id
            )
            VALUES (
                v_tenant_id,
                p_branch_id,
                'branch_hours.branch_changed',
                pg_catalog.jsonb_build_object(
                    'branch_id', p_branch_id,
                    'reason', p_reason,
                    'correlation_id', v_correlation_id
                ),
                v_dedupe_key,
                1,
                v_correlation_id,
                p_available_at,
                p_parent_event_id
            )
            ON CONFLICT (event_type, dedupe_key) DO NOTHING
            RETURNING id INTO v_event_id;

            IF v_event_id IS NULL THEN
                SELECT id INTO v_event_id
                FROM public.transactional_outbox
                WHERE event_type = 'branch_hours.branch_changed'
                  AND dedupe_key = v_dedupe_key;
            END IF;
            RETURN v_event_id;
        END;
        $function$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text) FROM PUBLIC"
    )
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text) "
        "OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text) TO worker_runtime"
    )
    op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    if bind.execute(
        sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
        {"signature": _CHILD_FUNCTION},
    ).scalar_one():
        raise RuntimeError("0a1b child enqueue function already exists")
    if "branch_hours_worker_outbox_insert" not in _policy_names(bind, _QUEUE):
        raise RuntimeError("0a1b predecessor worker queue INSERT policy is absent")
    for collision in (
        "branch_hours_internal_outbox_read",
        "branch_hours_internal_child_outbox_insert",
    ):
        if collision in _policy_names(bind, _QUEUE):
            raise RuntimeError(f"0a1b policy collision: {collision}")

    for relation, names in _SOURCE_POLICIES.items():
        expected = {names} if isinstance(names, str) else set(names)
        if not expected.issubset(_policy_names(bind, relation)):
            raise RuntimeError(f"0a1b predecessor source policy drift on {relation}")

    # Remove direct event creation and broad row mutation from the worker.
    op.execute("REVOKE INSERT, UPDATE ON TABLE public.transactional_outbox FROM worker_runtime")
    op.execute(
        "GRANT UPDATE (leased_by, leased_until, delivery_attempts, last_error, "
        "processed_at, dead_lettered_at, available_at) "
        "ON TABLE public.transactional_outbox TO worker_runtime"
    )
    op.execute("DROP POLICY branch_hours_worker_outbox_insert ON public.transactional_outbox")

    _drop_source_policies()
    _create_lease_bound_source_policies()
    _create_child_function()

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.transactional_outbox', 'INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("0a1b worker retained direct queue INSERT")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.transactional_outbox', 'DELETE') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.transactional_outbox', 'TRUNCATE')"
        )
    ).scalar_one():
        raise RuntimeError("0a1b worker has destructive queue capability")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("0a1b leaked app_security_owner public CREATE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION public.enqueue_branch_hours_child(uuid,uuid,uuid,timestamptz,text)"
    )
    op.execute("RESET ROLE")
    op.execute(
        "DROP POLICY branch_hours_internal_child_outbox_insert ON public.transactional_outbox"
    )
    op.execute(
        "DROP POLICY branch_hours_internal_outbox_read ON public.transactional_outbox"
    )
    op.execute(
        "REVOKE SELECT (id, tenant_id, branch_id, event_type, dedupe_key, correlation_id, "
        "leased_by, leased_until, processed_at, dead_lettered_at) "
        "ON TABLE public.transactional_outbox FROM app_security_owner"
    )

    _drop_source_policies()
    _create_e7_source_policies()

    op.execute(
        "REVOKE UPDATE (leased_by, leased_until, delivery_attempts, last_error, "
        "processed_at, dead_lettered_at, available_at) "
        "ON TABLE public.transactional_outbox FROM worker_runtime"
    )
    op.execute("GRANT INSERT, UPDATE ON TABLE public.transactional_outbox TO worker_runtime")
    op.execute(
        """
        CREATE POLICY branch_hours_worker_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO worker_runtime
        WITH CHECK (
            event_type = 'branch_hours.branch_changed'
            AND branch_id IS NOT NULL
            AND parent_event_id IS NOT NULL
            AND event_version = 1
        )
        """
    )
