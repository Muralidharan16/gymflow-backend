"""Establish the least-privilege branch lifecycle runtime security boundary.

Revision ID: 8192a3b4c5d6
Revises: 708192a3b4c5
Create Date: 2026-08-10

The lifecycle control-plane tables predate the reduced application database
identity. The service legitimately reads the immutable lifecycle catalogs and,
inside a tenant-scoped transition, appends history/events/outbox/watchdog rows.
The predecessor RLS policies were role-only on several child tables and did not
match the application's canonical ``admin`` role. They also left three tenant
relations without FORCE RLS and gave system-style GUC roles a cross-tenant
branch-state update escape.

This revision makes the database contract match the application architecture:

* app_runtime gets SELECT only on the three global lifecycle catalogs;
* app_runtime gets SELECT + INSERT only on tenant lifecycle ledgers/outbox/alerts;
* branch-state UPDATE remains owned by the earlier branch runtime boundary;
* every tenant lifecycle relation is ENABLE + FORCE RLS;
* lifecycle child policies prove tenant ownership through ``org_branches``;
* branch-state policies always require the current tenant, including system
  orchestration roles; and
* the legacy ``org_admin`` transition seed is bridged with canonical ``admin``
  without deleting the compatibility value.

No runtime role receives DELETE, TRUNCATE, REFERENCES, TRIGGER, table ownership,
schema CREATE, or RLS bypass capability. Downgrade restores the exact policy/
FORCE-RLS/ACL/data delta owned by this revision.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8192a3b4c5d6"
down_revision = "708192a3b4c5"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_RUNTIME_ROLE = "app_runtime"

_REFERENCE_TABLES = (
    "public.branch_status_definitions",
    "public.branch_status_transitions",
    "public.branch_deactivation_policies",
)
_TENANT_APPEND_TABLES = (
    "public.branch_status_history",
    "public.branch_lifecycle_events",
    "public.branch_outbox_events",
    "public.branch_watchdog_alerts",
)
_BRANCH_STATE = "public.org_branch_state"
_ALL_RELATIONS = _REFERENCE_TABLES + _TENANT_APPEND_TABLES + (_BRANCH_STATE,)

_FORBIDDEN_RUNTIME = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
_TENANT_SETTING = "NULLIF(current_setting('app.current_org_id', true), '')::UUID"

_PREDECESSOR_POLICY_NAMES = {
    _BRANCH_STATE: {
        "p_branch_select",
        "p_branch_update",
        "p_branch_insert",
        "p_branch_delete",
    },
    "public.branch_status_history": {"p_history_select"},
    "public.branch_lifecycle_events": {"p_events_insert", "p_events_select"},
    "public.branch_outbox_events": {
        "p_outbox_insert",
        "p_outbox_update",
        "p_outbox_select",
    },
    "public.branch_watchdog_alerts": {
        "p_watchdog_insert",
        "p_watchdog_update",
        "p_watchdog_select",
    },
}

_FORWARD_POLICY_NAMES = {
    _BRANCH_STATE: _PREDECESSOR_POLICY_NAMES[_BRANCH_STATE],
    "public.branch_status_history": {"p_history_select", "p_history_insert"},
    "public.branch_lifecycle_events": {"p_events_insert", "p_events_select"},
    "public.branch_outbox_events": {"p_outbox_insert", "p_outbox_select"},
    "public.branch_watchdog_alerts": {"p_watchdog_insert", "p_watchdog_select"},
}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _direct_privileges(bind, role_name: str, relation: str) -> set[str]:
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


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT policy_data.polname::text
                FROM pg_catalog.pg_policy AS policy_data
                WHERE policy_data.polrelid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _require_identity_and_runtime_role(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
                current_user::text AS current_name,
                role_data.rolsuper,
                role_data.rolinherit,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if (
        identity["session_name"] != _MIGRATION_OWNER
        or identity["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("lifecycle runtime migration requires migration_owner")
    if any(
        bool(identity[key])
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

    runtime = bind.execute(
        sa.text(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolinherit,
                rolcreatedb,
                rolcreaterole,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _RUNTIME_ROLE},
    ).mappings().one_or_none()
    if runtime is None:
        raise RuntimeError("required app_runtime role is missing")
    if any(bool(runtime[key]) for key in runtime):
        raise RuntimeError(
            "app_runtime violates NOLOGIN/NOINHERIT/NOBYPASSRLS reduced-role contract"
        )


def _require_relation_owners(bind) -> None:
    for relation in _ALL_RELATIONS:
        owner_name = _scalar(
            bind,
            """
            SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text
            FROM pg_catalog.pg_class AS relation_data
            WHERE relation_data.oid = CAST(:relation AS regclass)
            """,
            {"relation": relation},
        )
        if owner_name != _MIGRATION_OWNER:
            raise RuntimeError(
                f"unexpected owner for {relation}: {owner_name!r}"
            )


def _require_no_public_table_privileges(bind) -> None:
    forbidden = {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }
    for relation in _ALL_RELATIONS:
        schema_name, relation_name = relation.split(".", 1)
        observed = set(
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
                    WHERE namespace_data.nspname = :schema_name
                      AND relation_data.relname = :relation_name
                      AND acl.grantee = 0
                    """
                ),
                {"schema_name": schema_name, "relation_name": relation_name},
            ).scalars().all()
        )
        leaked = observed & forbidden
        if leaked:
            raise RuntimeError(
                f"PUBLIC unexpectedly has {sorted(leaked)!r} on {relation}"
            )


def _require_predecessor_security(bind) -> None:
    expected_rls = {
        _BRANCH_STATE: (True, True),
        "public.branch_status_history": (True, True),
        "public.branch_lifecycle_events": (True, False),
        "public.branch_outbox_events": (True, False),
        "public.branch_watchdog_alerts": (True, False),
    }
    for relation, expected in expected_rls.items():
        row = bind.execute(
            sa.text(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_catalog.pg_class
                WHERE oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).one()
        observed = (bool(row[0]), bool(row[1]))
        if observed != expected:
            raise RuntimeError(
                f"predecessor RLS drift for {relation}: "
                f"expected={expected!r}, observed={observed!r}"
            )

    for relation, expected_names in _PREDECESSOR_POLICY_NAMES.items():
        observed = _policy_names(bind, relation)
        if observed != expected_names:
            raise RuntimeError(
                f"predecessor policy inventory drift for {relation}: "
                f"expected={sorted(expected_names)!r}, observed={sorted(observed)!r}"
            )

    for relation in _REFERENCE_TABLES + _TENANT_APPEND_TABLES:
        observed = _direct_privileges(bind, _RUNTIME_ROLE, relation)
        if observed:
            raise RuntimeError(
                f"lifecycle predecessor ACL drift: app_runtime has "
                f"{sorted(observed)!r} on {relation}"
            )

    mixed_admin_rows = int(
        _scalar(
            bind,
            """
            SELECT count(*)
            FROM public.branch_status_transitions
            WHERE 'org_admin' = ANY(allowed_roles)
              AND 'admin' = ANY(allowed_roles)
            """,
        )
    )
    if mixed_admin_rows != 0:
        raise RuntimeError(
            "predecessor lifecycle role data already contains mixed admin/org_admin rows"
        )


def _drop_predecessor_policies() -> None:
    for statement in (
        "DROP POLICY p_branch_select ON public.org_branch_state",
        "DROP POLICY p_branch_update ON public.org_branch_state",
        "DROP POLICY p_branch_insert ON public.org_branch_state",
        "DROP POLICY p_branch_delete ON public.org_branch_state",
        "DROP POLICY p_history_select ON public.branch_status_history",
        "DROP POLICY p_events_insert ON public.branch_lifecycle_events",
        "DROP POLICY p_events_select ON public.branch_lifecycle_events",
        "DROP POLICY p_outbox_insert ON public.branch_outbox_events",
        "DROP POLICY p_outbox_update ON public.branch_outbox_events",
        "DROP POLICY p_outbox_select ON public.branch_outbox_events",
        "DROP POLICY p_watchdog_insert ON public.branch_watchdog_alerts",
        "DROP POLICY p_watchdog_update ON public.branch_watchdog_alerts",
        "DROP POLICY p_watchdog_select ON public.branch_watchdog_alerts",
    ):
        op.execute(statement)


def _create_forward_policies() -> None:
    tenant = _TENANT_SETTING
    branch_roles = (
        "'owner','admin','org_admin','compliance','superadmin',"
        "'system','saga_orchestrator','system_watchdog'"
    )
    append_roles = (
        "'owner','admin','org_admin','compliance','superadmin',"
        "'system','saga_orchestrator','system_watchdog'"
    )

    op.execute(
        f"""
        CREATE POLICY p_branch_select ON public.org_branch_state
        FOR SELECT USING (
            org_id = {tenant}
            AND (
                (auth.role() IN ('manager','trainer') AND is_operational = TRUE)
                OR (auth.role() IN ('owner','admin','org_admin')
                    AND status != 'permanently_closed')
                OR auth.role() IN ('compliance','superadmin','system',
                                   'saga_orchestrator','system_watchdog')
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_update ON public.org_branch_state
        FOR UPDATE
        USING (
            org_id = {tenant}
            AND auth.role() IN ({branch_roles})
        )
        WITH CHECK (
            org_id = {tenant}
            AND auth.role() IN ({branch_roles})
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_insert ON public.org_branch_state
        FOR INSERT WITH CHECK (
            org_id = {tenant}
            AND auth.role() IN ('owner','admin','org_admin','superadmin','system')
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_branch_delete ON public.org_branch_state
        FOR DELETE USING (
            org_id = {tenant}
            AND auth.role() = 'superadmin'
        )
        """
    )

    for relation, prefix in (
        ("public.branch_status_history", "history"),
        ("public.branch_lifecycle_events", "events"),
        ("public.branch_outbox_events", "outbox"),
        ("public.branch_watchdog_alerts", "watchdog"),
    ):
        short_name = relation.split(".", 1)[1]
        tenant_expr = (
            "EXISTS (SELECT 1 FROM public.org_branches AS tenant_branch "
            f"WHERE tenant_branch.id = {short_name}.branch_id "
            f"AND tenant_branch.org_id = {tenant})"
        )
        op.execute(
            f"""
            CREATE POLICY p_{prefix}_select ON {relation}
            FOR SELECT USING (
                {tenant_expr}
                AND auth.role() IN ({append_roles})
            )
            """
        )
        op.execute(
            f"""
            CREATE POLICY p_{prefix}_insert ON {relation}
            FOR INSERT WITH CHECK (
                {tenant_expr}
                AND auth.role() IN ({append_roles})
            )
            """
        )


def _grant_forward_acl() -> None:
    for relation in _REFERENCE_TABLES:
        op.execute(f"GRANT SELECT ON TABLE {relation} TO app_runtime")
    for relation in _TENANT_APPEND_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE {relation} TO app_runtime")


def _verify_forward(bind) -> None:
    for relation in (_BRANCH_STATE,) + _TENANT_APPEND_TABLES:
        row = bind.execute(
            sa.text(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_catalog.pg_class
                WHERE oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).one()
        if (bool(row[0]), bool(row[1])) != (True, True):
            raise RuntimeError(
                f"forward lifecycle RLS contract is not ENABLE+FORCE on {relation}"
            )

    for relation, expected_names in _FORWARD_POLICY_NAMES.items():
        observed = _policy_names(bind, relation)
        if observed != expected_names:
            raise RuntimeError(
                f"forward policy inventory drift for {relation}: "
                f"expected={sorted(expected_names)!r}, observed={sorted(observed)!r}"
            )

    for relation in _REFERENCE_TABLES:
        observed = _direct_privileges(bind, _RUNTIME_ROLE, relation)
        if observed != {"SELECT"}:
            raise RuntimeError(
                f"app_runtime reference ACL drift on {relation}: {sorted(observed)!r}"
            )

    for relation in _TENANT_APPEND_TABLES:
        observed = _direct_privileges(bind, _RUNTIME_ROLE, relation)
        if observed != {"SELECT", "INSERT"}:
            raise RuntimeError(
                f"app_runtime append ACL drift on {relation}: {sorted(observed)!r}"
            )
        for privilege in _FORBIDDEN_RUNTIME | {"UPDATE"}:
            if _scalar(
                bind,
                """
                SELECT pg_catalog.has_table_privilege(
                    CAST(:role_name AS name), :relation, :privilege
                )
                """,
                {
                    "role_name": _RUNTIME_ROLE,
                    "relation": relation,
                    "privilege": privilege,
                },
            ):
                raise RuntimeError(
                    f"app_runtime has forbidden {privilege} on {relation}"
                )

    for relation in _REFERENCE_TABLES:
        for privilege in _FORBIDDEN_RUNTIME | {"INSERT", "UPDATE"}:
            if _scalar(
                bind,
                """
                SELECT pg_catalog.has_table_privilege(
                    CAST(:role_name AS name), :relation, :privilege
                )
                """,
                {
                    "role_name": _RUNTIME_ROLE,
                    "relation": relation,
                    "privilege": privilege,
                },
            ):
                raise RuntimeError(
                    f"app_runtime has forbidden {privilege} on {relation}"
                )

    if _scalar(
        bind,
        """
        SELECT pg_catalog.has_schema_privilege(
            CAST(:role_name AS name), 'public', 'CREATE'
        )
        """,
        {"role_name": _RUNTIME_ROLE},
    ):
        raise RuntimeError("app_runtime must not have CREATE on public schema")

    missing_admin_bridge = int(
        _scalar(
            bind,
            """
            SELECT count(*)
            FROM public.branch_status_transitions
            WHERE 'org_admin' = ANY(allowed_roles)
              AND NOT ('admin' = ANY(allowed_roles))
            """,
        )
    )
    if missing_admin_bridge != 0:
        raise RuntimeError("lifecycle admin compatibility bridge is incomplete")


def _drop_forward_policies() -> None:
    for statement in (
        "DROP POLICY p_branch_select ON public.org_branch_state",
        "DROP POLICY p_branch_update ON public.org_branch_state",
        "DROP POLICY p_branch_insert ON public.org_branch_state",
        "DROP POLICY p_branch_delete ON public.org_branch_state",
        "DROP POLICY p_history_select ON public.branch_status_history",
        "DROP POLICY p_history_insert ON public.branch_status_history",
        "DROP POLICY p_events_select ON public.branch_lifecycle_events",
        "DROP POLICY p_events_insert ON public.branch_lifecycle_events",
        "DROP POLICY p_outbox_select ON public.branch_outbox_events",
        "DROP POLICY p_outbox_insert ON public.branch_outbox_events",
        "DROP POLICY p_watchdog_select ON public.branch_watchdog_alerts",
        "DROP POLICY p_watchdog_insert ON public.branch_watchdog_alerts",
    ):
        op.execute(statement)


def _create_predecessor_policies() -> None:
    op.execute(
        """
        CREATE POLICY p_branch_select ON public.org_branch_state FOR SELECT USING (
            org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID AND (
                (auth.role() IN ('manager', 'trainer') AND is_operational = TRUE) OR
                (auth.role() IN ('owner', 'org_admin') AND status != 'permanently_closed') OR
                auth.role() IN ('compliance', 'superadmin')
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY p_branch_update ON public.org_branch_state FOR UPDATE USING (
            (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
             AND auth.role() IN ('owner', 'org_admin', 'compliance', 'superadmin'))
            OR auth.role() IN ('system', 'saga_orchestrator', 'system_watchdog')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY p_branch_insert ON public.org_branch_state FOR INSERT WITH CHECK (
            auth.role() IN ('superadmin', 'system')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY p_branch_delete ON public.org_branch_state FOR DELETE USING (
            auth.role() = 'superadmin'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY p_history_select ON public.branch_status_history FOR SELECT USING (
            auth.role() IN ('owner', 'org_admin', 'compliance', 'superadmin')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY p_outbox_insert ON public.branch_outbox_events
        FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_outbox_update ON public.branch_outbox_events
        FOR UPDATE USING (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_outbox_select ON public.branch_outbox_events
        FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_events_insert ON public.branch_lifecycle_events
        FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_events_select ON public.branch_lifecycle_events
        FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_watchdog_insert ON public.branch_watchdog_alerts
        FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_watchdog_update ON public.branch_watchdog_alerts
        FOR UPDATE USING (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'))
        """
    )
    op.execute(
        """
        CREATE POLICY p_watchdog_select ON public.branch_watchdog_alerts
        FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'))
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_runtime_role(bind)
    _require_relation_owners(bind)
    _require_no_public_table_privileges(bind)
    _require_predecessor_security(bind)

    _drop_predecessor_policies()

    for relation in _TENANT_APPEND_TABLES:
        op.execute(f"ALTER TABLE {relation} FORCE ROW LEVEL SECURITY")

    _create_forward_policies()
    _grant_forward_acl()

    op.execute(
        """
        UPDATE public.branch_status_transitions
        SET allowed_roles = array_append(allowed_roles, 'admin')
        WHERE 'org_admin' = ANY(allowed_roles)
          AND NOT ('admin' = ANY(allowed_roles))
        """
    )

    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_runtime_role(bind)
    _require_relation_owners(bind)
    _require_no_public_table_privileges(bind)
    _verify_forward(bind)

    op.execute(
        """
        UPDATE public.branch_status_transitions
        SET allowed_roles = array_remove(allowed_roles, 'admin')
        WHERE 'org_admin' = ANY(allowed_roles)
          AND 'admin' = ANY(allowed_roles)
        """
    )

    for relation in _REFERENCE_TABLES:
        op.execute(f"REVOKE SELECT ON TABLE {relation} FROM app_runtime")
    for relation in _TENANT_APPEND_TABLES:
        op.execute(f"REVOKE SELECT, INSERT ON TABLE {relation} FROM app_runtime")

    _drop_forward_policies()
    _create_predecessor_policies()

    for relation in (
        "public.branch_lifecycle_events",
        "public.branch_outbox_events",
        "public.branch_watchdog_alerts",
    ):
        op.execute(f"ALTER TABLE {relation} NO FORCE ROW LEVEL SECURITY")

    _require_predecessor_security(bind)
