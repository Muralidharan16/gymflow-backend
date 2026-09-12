"""Break branch-hours worker RLS recursion with bounded lease predicates.

Revision ID: 60718293a4b5
Revises: 5f60718293a4
Create Date: 2026-08-11

The 0a1b worker policies embedded live-lease subqueries directly inside every
source-table policy.  Once the lifecycle worker policies were layered onto the
same FORCE-RLS branch surfaces, PostgreSQL could re-enter ``org_branches`` while
rewriting nested policy queries and fail with ``infinite recursion detected in
policy for relation org_branches``.

This revision removes cross-FORCE-RLS traversal from the worker policy graph.
Queue lease evaluation is performed by a fixed SECURITY DEFINER predicate owned
by ``app_security_owner``.  Tables that do not carry ``org_id`` use a second
predicate which first proves a live branch-hours lease and only then validates
branch->tenant lineage while executing as that bounded security owner.  The
worker receives EXECUTE only; no new table, schema, tenant-root, destructive or
BYPASSRLS capability is introduced.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "60718293a4b5"
down_revision = "5f60718293a4"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_MAINTENANCE_TOKEN = "branch_hours_projection"
_LEASE_FUNCTION = "public.branch_hours_worker_has_live_lease(uuid,uuid)"
_BRANCH_FUNCTION = "public.branch_hours_worker_can_access_branch(uuid,uuid)"

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
        raise RuntimeError("607182 branch-hours RLS recursion fix requires migration_owner")
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


def _uuid_guc(name: str) -> str:
    return f"""
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('{name}', true), ''),
                'uuid'
            )
            THEN CAST(NULLIF(pg_catalog.current_setting('{name}', true), '') AS uuid)
            ELSE CAST(NULL AS uuid)
        END
    """


def _drop_source_policies() -> None:
    for relation, names in _SOURCE_POLICIES.items():
        iterable = (names,) if isinstance(names, str) else names
        for name in iterable:
            op.execute(f"DROP POLICY {name} ON {relation}")


def _create_bounded_functions() -> None:
    # app_security_owner intentionally has no standing CREATE on public.  Open
    # the smallest transaction-scoped window required to create its own helper
    # functions, then close it before any postcondition is evaluated.
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        f"""
        CREATE FUNCTION public.branch_hours_worker_has_live_lease(
            p_tenant_id uuid,
            p_branch_id uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_current_org uuid;
            v_worker_id uuid;
        BEGIN
            IF NULLIF(pg_catalog.current_setting('app.internal_maintenance', true), '')
                   IS DISTINCT FROM '{_MAINTENANCE_TOKEN}'
               OR p_tenant_id IS NULL
            THEN
                RETURN FALSE;
            END IF;

            IF NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            ) OR NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.worker_id', true), ''),
                'uuid'
            ) THEN
                RETURN FALSE;
            END IF;

            v_current_org := CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid
            );
            v_worker_id := CAST(
                NULLIF(pg_catalog.current_setting('app.worker_id', true), '') AS uuid
            );
            IF p_tenant_id IS DISTINCT FROM v_current_org THEN
                RETURN FALSE;
            END IF;

            RETURN EXISTS (
                SELECT 1
                FROM public.transactional_outbox AS lease_data
                WHERE lease_data.tenant_id = p_tenant_id
                  AND lease_data.event_type IN (
                        'branch_hours.branch_changed',
                        'branch_hours.organization_changed'
                  )
                  AND lease_data.leased_by = v_worker_id
                  AND lease_data.leased_until > pg_catalog.clock_timestamp()
                  AND lease_data.processed_at IS NULL
                  AND lease_data.dead_lettered_at IS NULL
                  AND (
                        p_branch_id IS NULL
                        OR lease_data.event_type = 'branch_hours.organization_changed'
                        OR lease_data.branch_id = p_branch_id
                  )
            );
        END;
        $function$;
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.branch_hours_worker_can_access_branch(
            p_tenant_id uuid,
            p_branch_id uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        BEGIN
            IF p_branch_id IS NULL
               OR NOT public.branch_hours_worker_has_live_lease(
                    p_tenant_id, p_branch_id
               )
            THEN
                RETURN FALSE;
            END IF;

            RETURN EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = p_branch_id
                  AND branch_data.org_id = p_tenant_id
            );
        END;
        $function$;
        """
    )
    for signature in (_LEASE_FUNCTION, _BRANCH_FUNCTION):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO worker_runtime")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")


def _create_nonrecursive_source_policies() -> None:
    current_org = _uuid_guc("app.current_org_id")

    op.execute(
        """
        CREATE POLICY branch_hours_worker_branch_read
        ON public.org_branches
        FOR SELECT TO worker_runtime
        USING (public.branch_hours_worker_has_live_lease(org_id, id))
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_worker_branch_state_read
        ON public.org_branch_state
        FOR SELECT TO worker_runtime
        USING (public.branch_hours_worker_has_live_lease(org_id, branch_id))
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_worker_org_hours_read
        ON public.organization_operating_hours
        FOR SELECT TO worker_runtime
        USING (public.branch_hours_worker_has_live_lease(org_id, NULL))
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_hours_read
        ON public.branch_operating_hours
        FOR SELECT TO worker_runtime
        USING (
            public.branch_hours_worker_can_access_branch(
                {current_org}, branch_id
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_special_hours_read
        ON public.branch_special_hours
        FOR SELECT TO worker_runtime
        USING (
            public.branch_hours_worker_can_access_branch(
                {current_org}, branch_id
            )
        )
        """
    )
    projection_scope = f"""
        public.branch_hours_worker_can_access_branch(
            {current_org}, branch_hours_projection.branch_id
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


def _create_0a_predecessor_policies() -> None:
    current_org = _uuid_guc("app.current_org_id")
    worker_id = _uuid_guc("app.worker_id")
    maintenance = (
        "NULLIF(pg_catalog.current_setting('app.internal_maintenance', true), '') "
        "= 'branch_hours_projection'"
    )

    def live_lease(target_org: str, target_branch: str | None) -> str:
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

    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_read
        ON public.org_branches
        FOR SELECT TO worker_runtime
        USING (
            org_id = {current_org}
            AND {maintenance}
            AND {live_lease('org_id', 'id')}
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_state_read
        ON public.org_branch_state
        FOR SELECT TO worker_runtime
        USING (
            org_id = {current_org}
            AND {maintenance}
            AND {live_lease('org_id', 'branch_id')}
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_org_hours_read
        ON public.organization_operating_hours
        FOR SELECT TO worker_runtime
        USING (
            org_id = {current_org}
            AND {maintenance}
            AND {live_lease('org_id', None)}
        )
        """
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
                    SELECT 1
                    FROM public.org_branches AS branch_data
                    WHERE branch_data.id = {table_name}.branch_id
                      AND branch_data.org_id = {current_org}
                      AND {live_lease('branch_data.org_id', 'branch_data.id')}
                )
            )
            """
        )
    projection_scope = f"""
        {maintenance}
        AND EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            WHERE branch_data.id = branch_hours_projection.branch_id
              AND branch_data.org_id = {current_org}
              AND {live_lease('branch_data.org_id', 'branch_data.id')}
        )
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_read
        ON public.branch_hours_projection
        FOR SELECT TO worker_runtime USING ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_insert
        ON public.branch_hours_projection
        FOR INSERT TO worker_runtime WITH CHECK ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_update
        ON public.branch_hours_projection
        FOR UPDATE TO worker_runtime
        USING ({projection_scope}) WITH CHECK ({projection_scope})
        """
    )


def _function_contract(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(proowner)::text AS owner_name,
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
                       ) acl_data
                       WHERE acl_data.grantee = 0
                         AND acl_data.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc
            WHERE oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    for signature in (_LEASE_FUNCTION, _BRANCH_FUNCTION):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
            {"signature": signature},
        ).scalar_one():
            raise RuntimeError(f"607182 helper collision: {signature}")

    for relation, names in _SOURCE_POLICIES.items():
        expected = {names} if isinstance(names, str) else set(names)
        if not expected.issubset(_policy_names(bind, relation)):
            raise RuntimeError(f"607182 predecessor source policy drift on {relation}")

    # The helper owner already has exactly the source columns required by the
    # existing branch-hours child/enqueue boundaries. Refuse to broaden them.
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_column_privilege('app_security_owner', "
            "'public.transactional_outbox', 'tenant_id', 'SELECT') AND "
            "pg_catalog.has_column_privilege('app_security_owner', "
            "'public.transactional_outbox', 'leased_by', 'SELECT') AND "
            "pg_catalog.has_column_privilege('app_security_owner', "
            "'public.org_branches', 'id', 'SELECT') AND "
            "pg_catalog.has_column_privilege('app_security_owner', "
            "'public.org_branches', 'org_id', 'SELECT')"
        )
    ).scalar_one():
        raise RuntimeError("607182 security owner lacks predecessor bounded read surface")

    _drop_source_policies()
    _create_bounded_functions()
    _create_nonrecursive_source_policies()

    settings_required = {"search_path=pg_catalog, public", "row_security=on"}
    for signature in (_LEASE_FUNCTION, _BRANCH_FUNCTION):
        contract = _function_contract(bind, signature)
        settings = set(contract["proconfig"] or []) if contract else set()
        if (
            contract is None
            or contract["owner_name"] != _SECURITY_OWNER
            or not contract["prosecdef"]
            or contract["provolatile"] != "v"
            or not contract["worker_execute"]
            or contract["public_execute"]
            or not settings_required.issubset(settings)
        ):
            raise RuntimeError(
                f"607182 helper function contract drift: {signature}: {dict(contract or {})!r}"
            )

    for relation, names in _SOURCE_POLICIES.items():
        expected = {names} if isinstance(names, str) else set(names)
        if not expected.issubset(_policy_names(bind, relation)):
            raise RuntimeError(f"607182 failed to recreate source policy on {relation}")

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("607182 leaked app_security_owner public CREATE")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('worker_runtime', "
            "'public.transactional_outbox', 'INSERT') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.organizations', 'SELECT') OR "
            "pg_catalog.has_table_privilege('worker_runtime', "
            "'public.organizations', 'UPDATE')"
        )
    ).scalar_one():
        raise RuntimeError("607182 widened worker queue/tenant-root capability")


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)

    _drop_source_policies()
    _create_0a_predecessor_policies()

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION {_BRANCH_FUNCTION}")
    op.execute(f"DROP FUNCTION {_LEASE_FUNCTION}")
    op.execute("RESET ROLE")
