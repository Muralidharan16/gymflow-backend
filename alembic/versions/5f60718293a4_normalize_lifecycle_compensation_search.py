"""Normalize lifecycle compensation search commands from canonical status state.

Revision ID: 5f60718293a4
Revises: 4e5f60718293
Create Date: 2026-08-11

Dead-letter compensation restores the branch to ``from_status``.  Search
restoration must follow the canonical operational property of that status, not
a caller-provided event type.  This revision makes the security-owned lifecycle
child-command boundary authoritative for compensation direction: when the
payload declares ``saga_dead_letter_compensation``, the function resolves
``payload.status`` in ``branch_status_definitions`` and emits search_index only
for operational statuses, otherwise search_deindex.

The application worker still cannot read or mutate the status catalog through
this change.  Only ``app_security_owner`` receives SELECT(code,is_operational),
PUBLIC execution stays revoked, and the existing live-parent-lease / tenant /
branch lineage checks remain intact.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "5f60718293a4"
down_revision = "4e5f60718293"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_FUNCTION = "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid)"


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
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("5f607 lifecycle compensation migration requires migration_owner")
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


def _function_contract(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(proowner)::text AS owner_name,
                prosecdef,
                provolatile::text,
                proconfig,
                pg_catalog.has_function_privilege(
                    'worker_runtime', oid, 'EXECUTE'
                ) AS worker_execute,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(proacl, pg_catalog.acldefault('f', proowner))
                    ) AS acl_data
                    WHERE acl_data.grantee = 0
                      AND acl_data.privilege_type = 'EXECUTE'
                ) AS public_execute
            FROM pg_catalog.pg_proc
            WHERE oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": _FUNCTION},
    ).mappings().one_or_none()


def _create_function() -> None:
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
            v_effective_event_type text;
            v_origin_operational boolean;
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

            v_effective_event_type := p_event_type;
            IF p_payload ->> 'reason' = 'saga_dead_letter_compensation' THEN
                IF p_event_type NOT IN ('branch.search_index', 'branch.search_deindex')
                   OR NULLIF(p_payload ->> 'status', '') IS NULL
                THEN
                    RAISE EXCEPTION 'compensation requires a search event and origin status'
                        USING ERRCODE = '22023';
                END IF;

                SELECT status_data.is_operational
                INTO v_origin_operational
                FROM public.branch_status_definitions AS status_data
                WHERE status_data.code = p_payload ->> 'status';

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'unknown compensation origin status'
                        USING ERRCODE = '22023';
                END IF;

                v_effective_event_type := CASE
                    WHEN v_origin_operational THEN 'branch.search_index'
                    ELSE 'branch.search_deindex'
                END;
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
                v_effective_event_type,
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


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    predecessor = _function_contract(bind)
    if predecessor is None:
        raise RuntimeError("5f607 predecessor lifecycle child function is missing")
    if (
        predecessor["owner_name"] != _SECURITY_OWNER
        or not predecessor["prosecdef"]
        or predecessor["provolatile"] != "v"
        or not predecessor["worker_execute"]
        or predecessor["public_execute"]
    ):
        raise RuntimeError(
            f"5f607 predecessor lifecycle child function drifted: {dict(predecessor)!r}"
        )

    # The status catalog is global reference data; grant only the two columns
    # needed to canonicalize compensation direction.
    op.execute(
        "GRANT SELECT (code, is_operational) "
        "ON TABLE public.branch_status_definitions TO app_security_owner"
    )

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid)"
    )
    _create_function()
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) TO worker_runtime"
    )
    op.execute("RESET ROLE")

    final = _function_contract(bind)
    settings = set(final["proconfig"] or []) if final else set()
    if (
        final is None
        or final["owner_name"] != _SECURITY_OWNER
        or not final["prosecdef"]
        or final["provolatile"] != "v"
        or not final["worker_execute"]
        or final["public_execute"]
        or "search_path=pg_catalog, public" not in settings
        or "row_security=on" not in settings
    ):
        raise RuntimeError(
            f"5f607 final lifecycle child function drifted: {dict(final or {})!r}"
        )
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.branch_status_definitions', 'UPDATE') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.branch_status_definitions', 'INSERT')"
        )
    ).scalar_one():
        raise RuntimeError("5f607 leaked status catalog write capability to worker")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("5f607 leaked app_security_owner public CREATE")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    current = _function_contract(bind)
    if current is None or current["owner_name"] != _SECURITY_OWNER:
        raise RuntimeError("5f607 downgrade lifecycle child function drift")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid)"
    )
    # Recreate the 3d behavior exactly: caller-provided supported event type is
    # preserved after live-parent and lineage validation.
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
                outbox_id, tenant_id, branch_id, event_type, payload, created_at,
                process_after, status, attempt_count, max_attempts,
                correlation_id, leased_by, leased_until
            )
            VALUES (
                p_child_id, v_tenant_id, v_branch_id, p_event_type, p_payload,
                pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp(),
                'pending', 0, 5, v_correlation_id, NULL, NULL
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
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.enqueue_branch_lifecycle_child(uuid,uuid,text,jsonb,uuid) TO worker_runtime"
    )
    op.execute("RESET ROLE")
    op.execute(
        "REVOKE SELECT (code, is_operational) "
        "ON TABLE public.branch_status_definitions FROM app_security_owner"
    )
