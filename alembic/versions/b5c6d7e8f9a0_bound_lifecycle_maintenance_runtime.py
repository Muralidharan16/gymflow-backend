"""Isolate lifecycle maintenance from API/auth/worker database identities.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-11

Watchdog and reconciliation sweeps are deliberately cross-tenant maintenance
operations. They previously ran through the ordinary application database
identity, which forced app_runtime to retain table-wide UPDATE on branch state
and watchdog SELECT/INSERT even though request handlers no longer need those
maintenance capabilities.

The cluster bootstrap now provisions a dedicated NOLOGIN/NOBYPASSRLS
``lifecycle_maintenance_runtime`` capability role and deployment uses a distinct
maintenance login. This revision binds only the database delta required by that
role:

* app_runtime loses table-wide branch-state UPDATE and receives only the domain
  columns written by tenant-scoped branch/lifecycle request paths;
* app_runtime loses watchdog SELECT/INSERT and the corresponding tenant policies;
* lifecycle_maintenance_runtime receives branch-state SELECT plus UPDATE on only
  the five reconciliation-control columns;
* lifecycle_maintenance_runtime receives watchdog SELECT+INSERT only;
* all maintenance access is gated by transaction-local
  ``app.internal_maintenance = 'lifecycle'`` under existing FORCE RLS.

No role is created or altered by Alembic, no role membership is added, no object
ownership changes, and no BYPASSRLS/DDL/tenant-root/queue capability is granted.
Downgrade removes only this revision's maintenance delta and restores the exact
revision-90 app_runtime ACL/policy contract.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_API = "app_runtime"
_MAINTENANCE = "lifecycle_maintenance_runtime"

_STATE = "public.org_branch_state"
_WATCHDOG = "public.branch_watchdog_alerts"
_OUTBOX = "public.branch_outbox_events"
_ORGANIZATIONS = "public.organizations"
_OWNERS = "public.owners"
_AUTH_SESSIONS = "public.auth_sessions"

_API_STATE_COLUMNS = {
    "branch_status",
    "deleted_at",
    "is_active",
    "is_operational",
    "lifecycle_transition_in_progress",
    "saga_compensation_strategy",
    "saga_last_checkpoint",
    "status",
    "status_changed_at",
    "status_changed_by",
    "status_reason",
    "transition_source",
}

_MAINTENANCE_STATE_COLUMNS = {
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
    "search_last_synced_at",
    "search_sync_failed_at",
    "search_visibility_version",
}

_STATE_SELECT_POLICY = "lifecycle_maintenance_state_select"
_STATE_UPDATE_POLICY = "lifecycle_maintenance_state_update"
_WATCHDOG_SELECT_POLICY = "lifecycle_maintenance_watchdog_select"
_WATCHDOG_INSERT_POLICY = "lifecycle_maintenance_watchdog_insert"
_PREDECESSOR_WATCHDOG_SELECT = "p_watchdog_select"
_PREDECESSOR_WATCHDOG_INSERT = "p_watchdog_insert"

_MAINTENANCE_PREDICATE = (
    "current_setting('app.internal_maintenance'::text, true) = 'lifecycle'::text"
)


def _role_row(bind, role_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()


def _require_reduced_identities(bind) -> None:
    current = bind.execute(
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
    if (
        current["session_name"] != _MIGRATION_OWNER
        or current["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("b5c6 lifecycle maintenance migration requires migration_owner")
    if any(
        bool(current[key])
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

    for role_name in (_API, _MAINTENANCE):
        row = _role_row(bind, role_name)
        if row is None:
            raise RuntimeError(f"b5c6 required externally provisioned role is missing: {role_name}")
        if any(bool(value) for value in row.values()):
            raise RuntimeError(
                f"{role_name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS capability contract"
            )

    for capability in (_API, "auth_runtime", "worker_runtime", _MAINTENANCE):
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"),
            {"member": _MIGRATION_OWNER, "role": capability},
        ).scalar_one():
            raise RuntimeError(
                f"migration_owner must not be a member of runtime capability {capability}"
            )
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'SET')"),
            {"member": _MIGRATION_OWNER, "role": capability},
        ).scalar_one():
            raise RuntimeError(
                f"migration_owner must not SET ROLE to runtime capability {capability}"
            )

    for other_role in (
        _API,
        "auth_runtime",
        "worker_runtime",
        "app_security_owner",
        "app_rls_executor",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"),
            {"member": _MAINTENANCE, "role": other_role},
        ).scalar_one():
            raise RuntimeError(
                f"lifecycle maintenance must not inherit {other_role}"
            )
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member, :role, 'SET')"),
            {"member": _MAINTENANCE, "role": other_role},
        ).scalar_one():
            raise RuntimeError(
                f"lifecycle maintenance must not SET ROLE to {other_role}"
            )


def _require_force_rls(bind, relation: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": relation},
    ).one_or_none()
    if row is None or (bool(row[0]), bool(row[1])) != (True, True):
        raise RuntimeError(f"{relation} must retain ENABLE + FORCE RLS")


def _column_privileges(bind, role_name: str, relation: str, privilege: str) -> set[str]:
    schema_name, table_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = :schema_name
                  AND table_name = :table_name
                  AND grantee = :role_name
                  AND privilege_type = :privilege
                ORDER BY column_name
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "role_name": role_name,
                "privilege": privilege,
            },
        ).scalars().all()
    )


def _direct_relation_privileges(bind, role_name: str, relation: str) -> set[str]:
    schema_name, relation_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :relation_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
                "role_name": role_name,
            },
        ).scalars().all()
    )


def _policy_row(bind, relation: str, policy_name: str):
    return bind.execute(
        sa.text(
            """
            SELECT p.polcmd::text AS command,
                   p.polpermissive,
                   p.polroles,
                   pg_catalog.pg_get_expr(p.polqual, p.polrelid, true)::text AS using_expr,
                   pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid, true)::text AS check_expr
            FROM pg_catalog.pg_policy AS p
            WHERE p.polrelid = CAST(:relation AS regclass)
              AND p.polname = :policy_name
            """
        ),
        {"relation": relation, "policy_name": policy_name},
    ).mappings().one_or_none()


def _role_oid(bind, role_name: str) -> int:
    value = bind.execute(
        sa.text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role_name"),
        {"role_name": role_name},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"missing required role {role_name}")
    return int(value)


def _normalize_expression(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _require_predecessor(bind) -> None:
    _require_force_rls(bind, _STATE)
    _require_force_rls(bind, _WATCHDOG)

    if not bind.execute(
        sa.text("SELECT pg_catalog.has_table_privilege(:role, :relation, 'UPDATE')"),
        {"role": _API, "relation": _STATE},
    ).scalar_one():
        raise RuntimeError("b5c6 predecessor requires app_runtime table-wide state UPDATE")

    state_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'org_branch_state'
                """
            )
        ).scalars().all()
    )
    if _column_privileges(bind, _API, _STATE, "UPDATE") != state_columns:
        raise RuntimeError("b5c6 predecessor app_runtime UPDATE surface drifted")

    watchdog_acl = _direct_relation_privileges(bind, _API, _WATCHDOG)
    if watchdog_acl != {"SELECT", "INSERT"}:
        raise RuntimeError(
            f"b5c6 predecessor watchdog ACL drift: observed={sorted(watchdog_acl)!r}"
        )

    api_oid = _role_oid(bind, _API)
    for name, command in (
        (_PREDECESSOR_WATCHDOG_SELECT, "r"),
        (_PREDECESSOR_WATCHDOG_INSERT, "a"),
    ):
        row = _policy_row(bind, _WATCHDOG, name)
        if (
            row is None
            or row["command"] != command
            or not bool(row["polpermissive"])
            or list(row["polroles"]) != [api_oid]
        ):
            raise RuntimeError(f"b5c6 predecessor watchdog policy drift: {name}")

    if _direct_relation_privileges(bind, _MAINTENANCE, _STATE):
        raise RuntimeError("maintenance role already has table-level state ACL before b5c6")
    if _column_privileges(bind, _MAINTENANCE, _STATE, "UPDATE"):
        raise RuntimeError("maintenance role already has state column UPDATE before b5c6")
    if _direct_relation_privileges(bind, _MAINTENANCE, _WATCHDOG):
        raise RuntimeError("maintenance role already has watchdog ACL before b5c6")

    for relation in (_OUTBOX, _ORGANIZATIONS, _OWNERS, _AUTH_SESSIONS):
        if _direct_relation_privileges(bind, _MAINTENANCE, relation):
            raise RuntimeError(
                f"maintenance role has unexpected predecessor privileges on {relation}"
            )


def _drop_api_watchdog_policies() -> None:
    op.execute("DROP POLICY p_watchdog_select ON public.branch_watchdog_alerts")
    op.execute("DROP POLICY p_watchdog_insert ON public.branch_watchdog_alerts")


def _create_maintenance_policies() -> None:
    op.execute(
        """
        CREATE POLICY lifecycle_maintenance_state_select
        ON public.org_branch_state
        FOR SELECT TO lifecycle_maintenance_runtime
        USING (current_setting('app.internal_maintenance', true) = 'lifecycle')
        """
    )
    op.execute(
        """
        CREATE POLICY lifecycle_maintenance_state_update
        ON public.org_branch_state
        FOR UPDATE TO lifecycle_maintenance_runtime
        USING (current_setting('app.internal_maintenance', true) = 'lifecycle')
        WITH CHECK (current_setting('app.internal_maintenance', true) = 'lifecycle')
        """
    )
    op.execute(
        """
        CREATE POLICY lifecycle_maintenance_watchdog_select
        ON public.branch_watchdog_alerts
        FOR SELECT TO lifecycle_maintenance_runtime
        USING (current_setting('app.internal_maintenance', true) = 'lifecycle')
        """
    )
    op.execute(
        """
        CREATE POLICY lifecycle_maintenance_watchdog_insert
        ON public.branch_watchdog_alerts
        FOR INSERT TO lifecycle_maintenance_runtime
        WITH CHECK (current_setting('app.internal_maintenance', true) = 'lifecycle')
        """
    )


def _drop_maintenance_policies() -> None:
    for statement in (
        "DROP POLICY lifecycle_maintenance_state_select ON public.org_branch_state",
        "DROP POLICY lifecycle_maintenance_state_update ON public.org_branch_state",
        "DROP POLICY lifecycle_maintenance_watchdog_select ON public.branch_watchdog_alerts",
        "DROP POLICY lifecycle_maintenance_watchdog_insert ON public.branch_watchdog_alerts",
    ):
        op.execute(statement)


def _create_predecessor_watchdog_policies() -> None:
    tenant = "NULLIF(current_setting('app.current_org_id', true), '')::UUID"
    append_roles = (
        "'owner','admin','org_admin','compliance','superadmin',"
        "'system','saga_orchestrator','system_watchdog'"
    )
    tenant_expr = (
        "EXISTS (SELECT 1 FROM public.org_branches AS tenant_branch "
        "WHERE tenant_branch.id = branch_watchdog_alerts.branch_id "
        f"AND tenant_branch.org_id = {tenant})"
    )
    op.execute(
        f"""
        CREATE POLICY p_watchdog_select ON public.branch_watchdog_alerts
        FOR SELECT TO app_runtime USING (
            {tenant_expr}
            AND auth.role() IN ({append_roles})
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_watchdog_insert ON public.branch_watchdog_alerts
        FOR INSERT TO app_runtime WITH CHECK (
            {tenant_expr}
            AND auth.role() IN ({append_roles})
        )
        """
    )


def _require_forward(bind) -> None:
    _require_force_rls(bind, _STATE)
    _require_force_rls(bind, _WATCHDOG)

    if bind.execute(
        sa.text("SELECT pg_catalog.has_table_privilege(:role, :relation, 'UPDATE')"),
        {"role": _API, "relation": _STATE},
    ).scalar_one():
        raise RuntimeError("app_runtime retained table-wide state UPDATE")
    observed_api_columns = _column_privileges(bind, _API, _STATE, "UPDATE")
    if observed_api_columns != _API_STATE_COLUMNS:
        raise RuntimeError(
            "b5c6 API state UPDATE column drift: "
            f"expected={sorted(_API_STATE_COLUMNS)!r}, "
            f"observed={sorted(observed_api_columns)!r}"
        )
    leaked_maintenance_columns = observed_api_columns & _MAINTENANCE_STATE_COLUMNS
    if leaked_maintenance_columns:
        raise RuntimeError(
            "app_runtime retained maintenance state columns: "
            f"{sorted(leaked_maintenance_columns)!r}"
        )

    api_watchdog_acl = _direct_relation_privileges(bind, _API, _WATCHDOG)
    if api_watchdog_acl & {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"}:
        raise RuntimeError(
            f"app_runtime retained watchdog DML: {sorted(api_watchdog_acl)!r}"
        )
    for name in (_PREDECESSOR_WATCHDOG_SELECT, _PREDECESSOR_WATCHDOG_INSERT):
        if _policy_row(bind, _WATCHDOG, name) is not None:
            raise RuntimeError(f"retired API watchdog policy still exists: {name}")

    state_acl = _direct_relation_privileges(bind, _MAINTENANCE, _STATE)
    if state_acl != {"SELECT"}:
        raise RuntimeError(
            f"maintenance state table ACL drift: observed={sorted(state_acl)!r}"
        )
    observed_maintenance_columns = _column_privileges(
        bind, _MAINTENANCE, _STATE, "UPDATE"
    )
    if observed_maintenance_columns != _MAINTENANCE_STATE_COLUMNS:
        raise RuntimeError(
            "maintenance state UPDATE column drift: "
            f"expected={sorted(_MAINTENANCE_STATE_COLUMNS)!r}, "
            f"observed={sorted(observed_maintenance_columns)!r}"
        )
    if bind.execute(
        sa.text("SELECT pg_catalog.has_table_privilege(:role, :relation, 'UPDATE')"),
        {"role": _MAINTENANCE, "relation": _STATE},
    ).scalar_one():
        raise RuntimeError("maintenance role must not have table-wide state UPDATE")

    watchdog_acl = _direct_relation_privileges(bind, _MAINTENANCE, _WATCHDOG)
    if watchdog_acl != {"SELECT", "INSERT"}:
        raise RuntimeError(
            f"maintenance watchdog ACL drift: observed={sorted(watchdog_acl)!r}"
        )

    maintenance_oid = _role_oid(bind, _MAINTENANCE)
    for relation, name, command, qualifier in (
        (_STATE, _STATE_SELECT_POLICY, "r", "using_expr"),
        (_STATE, _STATE_UPDATE_POLICY, "w", "using_expr"),
        (_WATCHDOG, _WATCHDOG_SELECT_POLICY, "r", "using_expr"),
        (_WATCHDOG, _WATCHDOG_INSERT_POLICY, "a", "check_expr"),
    ):
        row = _policy_row(bind, relation, name)
        if (
            row is None
            or row["command"] != command
            or not bool(row["polpermissive"])
            or list(row["polroles"]) != [maintenance_oid]
            or _normalize_expression(row[qualifier]) != _MAINTENANCE_PREDICATE
        ):
            raise RuntimeError(f"maintenance policy contract drift: {relation}.{name}")
        if name == _STATE_UPDATE_POLICY:
            if _normalize_expression(row["check_expr"]) != _MAINTENANCE_PREDICATE:
                raise RuntimeError("maintenance state UPDATE WITH CHECK drifted")

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege(:role, 'public', 'CREATE')"
        ),
        {"role": _MAINTENANCE},
    ).scalar_one():
        raise RuntimeError("maintenance role must not have CREATE on public schema")

    forbidden = {
        _OUTBOX: {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"},
        _ORGANIZATIONS: {"INSERT", "UPDATE", "DELETE", "TRUNCATE"},
        _OWNERS: {"INSERT", "UPDATE", "DELETE", "TRUNCATE"},
        _AUTH_SESSIONS: {"INSERT", "UPDATE", "DELETE", "TRUNCATE"},
    }
    for relation, privileges in forbidden.items():
        for privilege in privileges:
            if bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege(:role, :relation, :privilege)"
                ),
                {
                    "role": _MAINTENANCE,
                    "relation": relation,
                    "privilege": privilege,
                },
            ).scalar_one():
                raise RuntimeError(
                    f"maintenance role has forbidden {privilege} on {relation}"
                )


def _require_downgrade_restored(bind) -> None:
    _require_force_rls(bind, _STATE)
    _require_force_rls(bind, _WATCHDOG)

    if not bind.execute(
        sa.text("SELECT pg_catalog.has_table_privilege(:role, :relation, 'UPDATE')"),
        {"role": _API, "relation": _STATE},
    ).scalar_one():
        raise RuntimeError("downgrade failed to restore app_runtime table-wide state UPDATE")

    watchdog_acl = _direct_relation_privileges(bind, _API, _WATCHDOG)
    if watchdog_acl != {"SELECT", "INSERT"}:
        raise RuntimeError(
            f"downgrade failed to restore API watchdog ACL: {sorted(watchdog_acl)!r}"
        )

    api_oid = _role_oid(bind, _API)
    for name, command in (
        (_PREDECESSOR_WATCHDOG_SELECT, "r"),
        (_PREDECESSOR_WATCHDOG_INSERT, "a"),
    ):
        row = _policy_row(bind, _WATCHDOG, name)
        if (
            row is None
            or row["command"] != command
            or not bool(row["polpermissive"])
            or list(row["polroles"]) != [api_oid]
        ):
            raise RuntimeError(f"downgrade failed to restore watchdog policy {name}")

    if _direct_relation_privileges(bind, _MAINTENANCE, _STATE):
        raise RuntimeError("downgrade left maintenance table-level state ACL")
    if _column_privileges(bind, _MAINTENANCE, _STATE, "UPDATE"):
        raise RuntimeError("downgrade left maintenance state column UPDATE")
    if _direct_relation_privileges(bind, _MAINTENANCE, _WATCHDOG):
        raise RuntimeError("downgrade left maintenance watchdog ACL")

    for relation, name in (
        (_STATE, _STATE_SELECT_POLICY),
        (_STATE, _STATE_UPDATE_POLICY),
        (_WATCHDOG, _WATCHDOG_SELECT_POLICY),
        (_WATCHDOG, _WATCHDOG_INSERT_POLICY),
    ):
        if _policy_row(bind, relation, name) is not None:
            raise RuntimeError(f"downgrade left maintenance policy {relation}.{name}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_reduced_identities(bind)
    _require_predecessor(bind)

    op.execute("REVOKE UPDATE ON TABLE public.org_branch_state FROM app_runtime")
    op.execute(
        "GRANT UPDATE (branch_status, deleted_at, is_active, is_operational, "
        "lifecycle_transition_in_progress, saga_compensation_strategy, "
        "saga_last_checkpoint, status, status_changed_at, status_changed_by, "
        "status_reason, transition_source) "
        "ON TABLE public.org_branch_state TO app_runtime"
    )

    op.execute(
        "REVOKE SELECT, INSERT ON TABLE public.branch_watchdog_alerts FROM app_runtime"
    )
    _drop_api_watchdog_policies()

    op.execute("GRANT USAGE ON SCHEMA public TO lifecycle_maintenance_runtime")
    op.execute("GRANT SELECT ON TABLE public.org_branch_state TO lifecycle_maintenance_runtime")
    op.execute(
        "GRANT UPDATE (reconciliation_claimed_at, reconciliation_claimed_by, "
        "search_last_synced_at, search_sync_failed_at, search_visibility_version) "
        "ON TABLE public.org_branch_state TO lifecycle_maintenance_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.branch_watchdog_alerts "
        "TO lifecycle_maintenance_runtime"
    )
    _create_maintenance_policies()

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_reduced_identities(bind)
    _require_forward(bind)

    _drop_maintenance_policies()
    op.execute(
        "REVOKE SELECT, INSERT ON TABLE public.branch_watchdog_alerts "
        "FROM lifecycle_maintenance_runtime"
    )
    op.execute(
        "REVOKE UPDATE (reconciliation_claimed_at, reconciliation_claimed_by, "
        "search_last_synced_at, search_sync_failed_at, search_visibility_version) "
        "ON TABLE public.org_branch_state FROM lifecycle_maintenance_runtime"
    )
    op.execute(
        "REVOKE SELECT ON TABLE public.org_branch_state FROM lifecycle_maintenance_runtime"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM lifecycle_maintenance_runtime")

    op.execute(
        "REVOKE UPDATE (branch_status, deleted_at, is_active, is_operational, "
        "lifecycle_transition_in_progress, saga_compensation_strategy, "
        "saga_last_checkpoint, status, status_changed_at, status_changed_by, "
        "status_reason, transition_source) "
        "ON TABLE public.org_branch_state FROM app_runtime"
    )
    op.execute("GRANT UPDATE ON TABLE public.org_branch_state TO app_runtime")

    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.branch_watchdog_alerts TO app_runtime"
    )
    _create_predecessor_watchdog_policies()

    _require_downgrade_restored(bind)
