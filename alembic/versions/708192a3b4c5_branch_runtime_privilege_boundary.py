"""Establish the least-privilege branch and lifecycle runtime boundary.

Revision ID: 708192a3b4c5
Revises: 6f708192a3b4
Create Date: 2026-08-10

The branch/lifecycle surfaces predate the reduced PostgreSQL runtime identities.
Once the application stopped connecting as an owner-equivalent login, several
legitimate production paths exposed missing or incomplete object/RLS contracts:

* steady-state branch APIs read branch metadata/state and the tenant-scoped WKT
  geolocation projection backing ordinary address latitude/longitude reads;
* verified onboarding creates the first principal branch and initial branch
  state through the dedicated auth/bootstrap pool;
* branch lifecycle transitions read immutable global lifecycle catalogs, update
  tenant branch state, and append tenant history/events/outbox/watchdog rows;
* legacy lifecycle child policies were role-only rather than tenant-scoped,
  three child relations were not FORCE RLS, and the branch-state UPDATE policy
  contained a cross-tenant system-role escape;
* the original permissive ``tenant_isolation_state`` ALL policy was tenant-only
  and therefore OR-combined with later permissive role policies, silently
  bypassing their role restrictions for callers that had table ACLs; and
* lifecycle seed data used legacy ``org_admin`` while the application canonical
  staff role is ``admin``.

This revision owns the complete unmerged correction. Ordinary runtime receives
only the operations exercised by branch/lifecycle application code. Lifecycle
append surfaces are SELECT+INSERT only; reference catalogs and geolocation are
read-only. Auth bootstrap receives only first-branch creation/linking rights.
Every tenant lifecycle relation is ENABLE + FORCE RLS and child rows prove
current-tenant ownership through ``org_branches``. The broad predecessor
``tenant_isolation_state`` policy is replaced by operation-specific tenant+role
policies, while downgrade recreates that predecessor policy exactly. The
compatibility value ``org_admin`` is preserved while canonical ``admin`` is
added to the same seed transitions.

No runtime role receives DELETE, TRUNCATE, REFERENCES, TRIGGER, schema CREATE,
ownership, SUPERUSER, INHERIT, or BYPASSRLS. Downgrade restores the exact ACL,
RLS-force, policy-inventory, and seed-data delta owned by this revision.
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "708192a3b4c5"
down_revision = "6f708192a3b4"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_RUNTIME_ROLE = "app_runtime"
_AUTH_ROLE = "auth_runtime"

_BRANCHES = "public.org_branches"
_BRANCH_STATE = "public.org_branch_state"
_GEOLOCATION_STATE = "public.branch_geolocation_state"

_LIFECYCLE_REFERENCE_TABLES = (
    "public.branch_status_definitions",
    "public.branch_status_transitions",
    "public.branch_deactivation_policies",
)
_LIFECYCLE_APPEND_TABLES = (
    "public.branch_status_history",
    "public.branch_lifecycle_events",
    "public.branch_outbox_events",
    "public.branch_watchdog_alerts",
)

_BASE_RELATIONS = (_BRANCHES, _BRANCH_STATE, _GEOLOCATION_STATE)
_ALL_RELATIONS = _BASE_RELATIONS + _LIFECYCLE_REFERENCE_TABLES + _LIFECYCLE_APPEND_TABLES
_GEOLOCATION_POLICY = "geolocation_state_tenant_isolation"
_STATE_TENANT_POLICY = "tenant_isolation_state"
_TENANT_EXPR = "org_id=nullifcurrent_setting'app.current_org_id'::text,true,''::text::uuid"

_RUNTIME_PRIVILEGES = {
    _BRANCHES: {"SELECT"},
    _BRANCH_STATE: {"SELECT", "UPDATE"},
    _GEOLOCATION_STATE: {"SELECT"},
    **{relation: {"SELECT"} for relation in _LIFECYCLE_REFERENCE_TABLES},
    **{relation: {"SELECT", "INSERT"} for relation in _LIFECYCLE_APPEND_TABLES},
}
_AUTH_BOOTSTRAP_PRIVILEGES = {
    _BRANCHES: {"INSERT", "UPDATE"},
    _BRANCH_STATE: {"INSERT"},
}

_FORBIDDEN_PRIVILEGES = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
_READ_ONLY_FORBIDDEN = _FORBIDDEN_PRIVILEGES | {"INSERT", "UPDATE"}
_APPEND_FORBIDDEN = _FORBIDDEN_PRIVILEGES | {"UPDATE"}

_PREDECESSOR_POLICY_NAMES = {
    _BRANCH_STATE: {
        "tenant_isolation_state",
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
    _BRANCH_STATE: {
        "p_branch_select",
        "p_branch_update",
        "p_branch_insert",
        "p_branch_delete",
    },
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


def _require_identity_and_roles(bind) -> None:
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
        raise RuntimeError("branch runtime migration requires migration_owner")
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

    rows = bind.execute(
        sa.text(
            """
            SELECT
                rolname,
                rolcanlogin,
                rolsuper,
                rolinherit,
                rolcreatedb,
                rolcreaterole,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:runtime_role, :auth_role)
            """
        ),
        {"runtime_role": _RUNTIME_ROLE, "auth_role": _AUTH_ROLE},
    ).mappings().all()
    by_name = {row["rolname"]: row for row in rows}
    if set(by_name) != {_RUNTIME_ROLE, _AUTH_ROLE}:
        raise RuntimeError("required branch runtime roles are missing")

    for role_name, row in by_name.items():
        if any(
            bool(row[key])
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolinherit",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise RuntimeError(
                f"managed role {role_name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS"
            )


def _require_relation_owners(bind) -> None:
    for relation in _ALL_RELATIONS:
        row = bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_get_userbyid(relation_data.relowner)::text AS owner_name
                FROM pg_catalog.pg_class AS relation_data
                WHERE relation_data.oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(f"required branch relation is missing: {relation}")
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"unexpected owner for {relation}: {row['owner_name']!r}"
            )


def _require_base_relation_security(bind) -> None:
    for relation in _BASE_RELATIONS:
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
                f"{relation} must retain ENABLE + FORCE ROW LEVEL SECURITY"
            )


def _normalized_tenant_expr(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s()]", "", str(value).lower())


def _require_geolocation_policy(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polname::text AS policy_name,
                policy_data.polcmd::text AS command,
                pg_catalog.pg_get_expr(
                    policy_data.polqual, policy_data.polrelid, true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck, policy_data.polrelid, true
                )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _GEOLOCATION_STATE},
    ).mappings().all()
    if len(rows) != 1 or rows[0]["policy_name"] != _GEOLOCATION_POLICY:
        raise RuntimeError(
            "branch geolocation policy inventory drifted: "
            f"{[row['policy_name'] for row in rows]!r}"
        )
    row = rows[0]
    if (
        row["command"] != "*"
        or _normalized_tenant_expr(row["using_expr"]) != _TENANT_EXPR
        or _normalized_tenant_expr(row["check_expr"]) != _TENANT_EXPR
    ):
        raise RuntimeError("branch geolocation tenant policy drifted")


def _require_predecessor_state_tenant_policy(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polcmd::text AS command,
                policy_data.polpermissive AS permissive,
                policy_data.polroles = ARRAY[0::oid] AS public_only,
                pg_catalog.pg_get_expr(
                    policy_data.polqual, policy_data.polrelid, true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck, policy_data.polrelid, true
                )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname = :policy_name
            """
        ),
        {"relation": _BRANCH_STATE, "policy_name": _STATE_TENANT_POLICY},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("predecessor tenant_isolation_state policy is missing")
    if (
        row["command"] != "*"
        or not bool(row["permissive"])
        or not bool(row["public_only"])
        or _normalized_tenant_expr(row["using_expr"]) != _TENANT_EXPR
        or row["check_expr"] is not None
    ):
        raise RuntimeError("predecessor tenant_isolation_state policy drifted")


def _require_no_public_dml(bind) -> None:
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


def _require_predecessor_lifecycle_security(bind) -> None:
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

    _require_predecessor_state_tenant_policy(bind)

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


def _require_predecessor_acl(bind) -> None:
    for role_name in (_RUNTIME_ROLE, _AUTH_ROLE):
        for relation in _ALL_RELATIONS:
            observed = _direct_privileges(bind, role_name, relation)
            if observed:
                raise RuntimeError(
                    "branch runtime predecessor ACL drift: "
                    f"{role_name} has {sorted(observed)!r} on {relation}"
                )


def _drop_predecessor_lifecycle_policies() -> None:
    for statement in (
        "DROP POLICY tenant_isolation_state ON public.org_branch_state",
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


def _create_forward_lifecycle_policies() -> None:
    tenant = "NULLIF(current_setting('app.current_org_id', true), '')::UUID"
    branch_roles = (
        "'owner','admin','org_admin','compliance','superadmin',"
        "'system','saga_orchestrator','system_watchdog'"
    )
    append_roles = branch_roles

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


def _drop_forward_lifecycle_policies() -> None:
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


def _create_predecessor_lifecycle_policies() -> None:
    op.execute(
        """
        CREATE POLICY tenant_isolation_state ON public.org_branch_state
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
        )
        """
    )
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


def _grant_contract() -> None:
    op.execute("GRANT SELECT ON TABLE public.org_branches TO app_runtime")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.org_branch_state TO app_runtime")
    op.execute("GRANT SELECT ON TABLE public.branch_geolocation_state TO app_runtime")

    for relation in _LIFECYCLE_REFERENCE_TABLES:
        op.execute(f"GRANT SELECT ON TABLE {relation} TO app_runtime")
    for relation in _LIFECYCLE_APPEND_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE {relation} TO app_runtime")

    op.execute("GRANT INSERT, UPDATE ON TABLE public.org_branches TO auth_runtime")
    op.execute("GRANT INSERT ON TABLE public.org_branch_state TO auth_runtime")


def _revoke_contract() -> None:
    op.execute("REVOKE SELECT ON TABLE public.org_branches FROM app_runtime")
    op.execute("REVOKE SELECT, UPDATE ON TABLE public.org_branch_state FROM app_runtime")
    op.execute("REVOKE SELECT ON TABLE public.branch_geolocation_state FROM app_runtime")

    for relation in _LIFECYCLE_REFERENCE_TABLES:
        op.execute(f"REVOKE SELECT ON TABLE {relation} FROM app_runtime")
    for relation in _LIFECYCLE_APPEND_TABLES:
        op.execute(f"REVOKE SELECT, INSERT ON TABLE {relation} FROM app_runtime")

    op.execute("REVOKE INSERT, UPDATE ON TABLE public.org_branches FROM auth_runtime")
    op.execute("REVOKE INSERT ON TABLE public.org_branch_state FROM auth_runtime")


def _verify_final_acl(bind) -> None:
    for role_name, contract in (
        (_RUNTIME_ROLE, _RUNTIME_PRIVILEGES),
        (_AUTH_ROLE, _AUTH_BOOTSTRAP_PRIVILEGES),
    ):
        for relation in _ALL_RELATIONS:
            expected = set(contract.get(relation, set()))
            observed = _direct_privileges(bind, role_name, relation)
            if observed != expected:
                raise RuntimeError(
                    "branch runtime direct ACL drift: "
                    f"{role_name} on {relation}: "
                    f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
                )

            for privilege in expected:
                if not _scalar(
                    bind,
                    """
                    SELECT pg_catalog.has_table_privilege(
                        CAST(:role_name AS name), :relation, :privilege
                    )
                    """,
                    {
                        "role_name": role_name,
                        "relation": relation,
                        "privilege": privilege,
                    },
                ):
                    raise RuntimeError(
                        f"{role_name} lacks required {privilege} on {relation}"
                    )

            if relation in _LIFECYCLE_REFERENCE_TABLES or relation == _GEOLOCATION_STATE:
                forbidden = _READ_ONLY_FORBIDDEN
            elif relation in _LIFECYCLE_APPEND_TABLES:
                forbidden = _APPEND_FORBIDDEN
            else:
                forbidden = _FORBIDDEN_PRIVILEGES

            for privilege in forbidden:
                if _scalar(
                    bind,
                    """
                    SELECT pg_catalog.has_table_privilege(
                        CAST(:role_name AS name), :relation, :privilege
                    )
                    """,
                    {
                        "role_name": role_name,
                        "relation": relation,
                        "privilege": privilege,
                    },
                ):
                    raise RuntimeError(
                        f"{role_name} has forbidden {privilege} on {relation}"
                    )

    for role_name in (_RUNTIME_ROLE, _AUTH_ROLE):
        if _scalar(
            bind,
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role_name AS name), 'public', 'CREATE'
            )
            """,
            {"role_name": role_name},
        ):
            raise RuntimeError(
                f"{role_name} must not have CREATE on public schema"
            )


def _verify_forward_lifecycle_security(bind) -> None:
    for relation in (_BRANCH_STATE,) + _LIFECYCLE_APPEND_TABLES:
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


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_roles(bind)
    _require_relation_owners(bind)
    _require_base_relation_security(bind)
    _require_geolocation_policy(bind)
    _require_no_public_dml(bind)
    _require_predecessor_lifecycle_security(bind)
    _require_predecessor_acl(bind)

    _drop_predecessor_lifecycle_policies()
    for relation in _LIFECYCLE_APPEND_TABLES:
        op.execute(f"ALTER TABLE {relation} FORCE ROW LEVEL SECURITY")
    _create_forward_lifecycle_policies()

    op.execute(
        """
        UPDATE public.branch_status_transitions
        SET allowed_roles = array_append(allowed_roles, 'admin')
        WHERE 'org_admin' = ANY(allowed_roles)
          AND NOT ('admin' = ANY(allowed_roles))
        """
    )

    _grant_contract()
    _verify_final_acl(bind)
    _verify_forward_lifecycle_security(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_roles(bind)
    _require_relation_owners(bind)
    _require_base_relation_security(bind)
    _require_geolocation_policy(bind)
    _require_no_public_dml(bind)
    _verify_final_acl(bind)
    _verify_forward_lifecycle_security(bind)

    _revoke_contract()

    op.execute(
        """
        UPDATE public.branch_status_transitions
        SET allowed_roles = array_remove(allowed_roles, 'admin')
        WHERE 'org_admin' = ANY(allowed_roles)
          AND 'admin' = ANY(allowed_roles)
        """
    )

    _drop_forward_lifecycle_policies()
    _create_predecessor_lifecycle_policies()
    for relation in (
        "public.branch_lifecycle_events",
        "public.branch_outbox_events",
        "public.branch_watchdog_alerts",
    ):
        op.execute(f"ALTER TABLE {relation} NO FORCE ROW LEVEL SECURITY")

    _require_predecessor_lifecycle_security(bind)
    _require_predecessor_acl(bind)
