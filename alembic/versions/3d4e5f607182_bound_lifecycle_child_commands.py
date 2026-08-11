"""Replace direct lifecycle worker outbox INSERT with a leased child-command function.

Revision ID: 3d4e5f607182
Revises: 2c3d4e5f6071
Create Date: 2026-08-11

A row-level policy on branch_outbox_events must not recursively query the same
RLS relation to validate its parent lease.  This revision removes direct worker
INSERT entirely.  A no-login app_security_owner SECURITY DEFINER function reads
the parent under its own bounded policy, verifies worker/tenant/branch/
correlation lineage and inserts only the four supported child command types.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f607182"
down_revision = "2c3d4e5f6071"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_OUTBOX = "public.branch_outbox_events"
_FUNCTION = "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid)"


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text, current_user::text,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = current_user
            """
        )
    ).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("3d4e lifecycle child boundary requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _policy_names(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                "SELECT polname::text FROM pg_catalog.pg_policy "
                "WHERE polrelid = CAST(:relation AS regclass)"
            ),
            {"relation": _OUTBOX},
        ).scalars().all()
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    if "lifecycle_worker_outbox_insert" not in _policy_names(bind):
        raise RuntimeError("3d4e predecessor worker INSERT policy is missing")
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
        {"signature": _FUNCTION},
    ).scalar_one():
        raise RuntimeError("3d4e lifecycle child function already exists")

    op.execute("DROP POLICY lifecycle_worker_outbox_insert ON public.branch_outbox_events")
    op.execute("REVOKE INSERT ON TABLE public.branch_outbox_events FROM worker_runtime")

    op.execute(
        """
        GRANT SELECT (
            outbox_id, tenant_id, branch_id, event_type, status, leased_by,
            leased_until, correlation_id
        ) ON TABLE public.branch_outbox_events TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT INSERT (
            outbox_id, tenant_id, branch_id, event_type, payload, created_at,
            process_after, status, attempt_count, max_attempts, correlation_id,
            leased_by, leased_until
        ) ON TABLE public.branch_outbox_events TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY lifecycle_internal_outbox_read
        ON public.branch_outbox_events
        FOR SELECT TO app_security_owner
        USING (TRUE)
        """
    )
    op.execute(
        """
        CREATE POLICY lifecycle_internal_child_insert
        ON public.branch_outbox_events
        FOR INSERT TO app_security_owner
        WITH CHECK (
            status = 'pending'
            AND attempt_count = 0
            AND leased_by IS NULL
            AND leased_until IS NULL
            AND event_type IN (
                'branch.search_deindex',
                'branch.search_index',
                'branch.member_notification',
                'branch.refund_required'
            )
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.enqueue_branch_lifecycle_child(
            p_parent_outbox_id uuid,
            p_worker_id uuid,
            p_event_type text,
            p_payload jsonb,
            p_child_id uuid
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
            v_branch_id uuid;
            v_correlation_id uuid;
            v_existing uuid;
        BEGIN
            IF p_parent_outbox_id IS NULL
               OR p_worker_id IS NULL
               OR p_child_id IS NULL
               OR p_payload IS NULL
               OR p_event_type NOT IN (
                    'branch.search_deindex',
                    'branch.search_index',
                    'branch.member_notification',
                    'branch.refund_required'
               )
            THEN
                RAISE EXCEPTION 'invalid lifecycle child command arguments'
                    USING ERRCODE = '22023';
            END IF;

            SELECT tenant_id, branch_id, correlation_id
            INTO v_tenant_id, v_branch_id, v_correlation_id
            FROM public.branch_outbox_events
            WHERE outbox_id = p_parent_outbox_id
              AND event_type = 'branch.lifecycle_saga'
              AND status = 'processing'
              AND leased_by = p_worker_id
              AND leased_until > pg_catalog.clock_timestamp();

            IF NOT FOUND THEN
                RAISE EXCEPTION 'lifecycle child command requires a live owned saga lease'
                    USING ERRCODE = '42501';
            END IF;

            IF p_payload ->> 'branch_id' IS DISTINCT FROM v_branch_id::text
               OR p_payload ->> 'org_id' IS DISTINCT FROM v_tenant_id::text
            THEN
                RAISE EXCEPTION 'lifecycle child payload tenant/branch lineage mismatch'
                    USING ERRCODE = '42501';
            END IF;

            SELECT outbox_id INTO v_existing
            FROM public.branch_outbox_events
            WHERE outbox_id = p_child_id;
            IF FOUND THEN
                RETURN v_existing;
            END IF;

            INSERT INTO public.branch_outbox_events (
                outbox_id,
                tenant_id,
                branch_id,
                event_type,
                payload,
                created_at,
                process_after,
                status,
                attempt_count,
                max_attempts,
                correlation_id,
                leased_by,
                leased_until
            )
            VALUES (
                p_child_id,
                v_tenant_id,
                v_branch_id,
                p_event_type,
                p_payload,
                pg_catalog.clock_timestamp(),
                pg_catalog.clock_timestamp(),
                'pending',
                0,
                5,
                v_correlation_id,
                NULL,
                NULL
            );
            RETURN p_child_id;
        END;
        $function$;
        """
    )

    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) FROM PUBLIC"
    )
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) "
        "OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) TO worker_runtime"
    )
    op.execute("RESET ROLE")

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.branch_outbox_events', 'INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("3d4e worker retained direct lifecycle outbox INSERT")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("3d4e leaked app_security_owner schema CREATE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid)"
    )
    op.execute("RESET ROLE")
    op.execute(
        "DROP POLICY lifecycle_internal_child_insert ON public.branch_outbox_events"
    )
    op.execute(
        "DROP POLICY lifecycle_internal_outbox_read ON public.branch_outbox_events"
    )
    op.execute(
        "REVOKE SELECT (outbox_id, tenant_id, branch_id, event_type, status, leased_by, "
        "leased_until, correlation_id) ON TABLE public.branch_outbox_events "
        "FROM app_security_owner"
    )
    op.execute(
        "REVOKE INSERT (outbox_id, tenant_id, branch_id, event_type, payload, created_at, "
        "process_after, status, attempt_count, max_attempts, correlation_id, leased_by, "
        "leased_until) ON TABLE public.branch_outbox_events FROM app_security_owner"
    )

    op.execute("GRANT INSERT ON TABLE public.branch_outbox_events TO worker_runtime")
    op.execute(
        """
        CREATE POLICY lifecycle_worker_outbox_insert
        ON public.branch_outbox_events
        FOR INSERT TO worker_runtime
        WITH CHECK (
            tenant_id = CASE
                WHEN pg_catalog.pg_input_is_valid(
                    NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                    'uuid'
                )
                THEN CAST(NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid)
                ELSE CAST(NULL AS uuid)
            END
            AND status = 'pending'
            AND attempt_count = 0
            AND leased_by IS NULL
            AND leased_until IS NULL
            AND event_type IN (
                'branch.search_deindex',
                'branch.search_index',
                'branch.member_notification',
                'branch.refund_required'
            )
        )
        """
    )
