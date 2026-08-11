"""Harden the branch-hours runtime and audit boundary.

Revision ID: b4c5d6e7f809
Revises: a3b4c5d6e7f8
Create Date: 2026-08-11

The branch-hours subsystem predates the reduced application database identity.
Its API requires ordinary SELECT/INSERT/UPDATE access to hours records, while
its audit trigger is an internal side effect that must not require application
runtime to append directly to the FORCE-RLS audit table.

The predecessor also contains two authorization defects:

* organization default-hours uses a tenant-only ALL policy, so database writes
  are not restricted to owner/admin even though the API is; and
* branch special-hours is FORCE-RLS protected but has no policies at all.

This revision establishes an explicit least-privilege contract:

* app_runtime receives only route-required SELECT/INSERT/UPDATE privileges;
* no runtime DELETE/TRUNCATE/REFERENCES/TRIGGER privilege is introduced;
* organization default writes require an active tenant member plus canonical
  owner/admin role;
* branch standard/special writes require an active tenant member plus either
  owner/admin role or an active branch-manager assignment;
* branch special-hours receives tenant-scoped read/write policies;
* audit rows are appended through a tenant-bound SECURITY DEFINER trigger
  owned by the NOLOGIN/NOBYPASSRLS app_security_owner, with only column-level
  INSERT capability and an explicit INSERT RLS policy; and
* app_runtime receives no direct audit-log INSERT capability.

Downgrade restores the exact DBEB policy/trigger shape and removes all ACL and
security objects owned by this revision.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f809"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RUNTIME = "app_runtime"
_SECURITY_OWNER = "app_security_owner"
_AUDIT_RUNTIME_SIGNATURE = "public.audit_branch_hours_runtime()"
_LEGACY_AUDIT_SIGNATURE = "app_private.audit_branch_hours()"

_RUNTIME_PRIVILEGES = {
    "public.organization_operating_hours": {"SELECT", "INSERT", "UPDATE"},
    "public.branch_operating_hours": {"SELECT", "INSERT", "UPDATE"},
    "public.branch_special_hours": {"SELECT", "INSERT", "UPDATE"},
    "public.branch_hours_projection": {"SELECT"},
}
_FORBIDDEN = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}

_PREDECESSOR_POLICIES = {
    "public.organization_operating_hours": {"tenant_isolation_org_hours"},
    "public.branch_operating_hours": {
        "tenant_isolation_read_hours",
        "write_branch_hours_org_admin",
        "write_branch_hours_manager",
    },
    "public.branch_special_hours": set(),
    "public.branch_hours_projection": {"tenant_isolation_projection"},
    "public.branch_hours_audit_log": {"tenant_isolation_audit"},
}

_FORWARD_POLICIES = {
    "public.organization_operating_hours": {
        "org_hours_read_active_member",
        "org_hours_insert_owner_admin",
        "org_hours_update_owner_admin",
    },
    "public.branch_operating_hours": {
        "branch_hours_read_active_member",
        "branch_hours_insert_authorized",
        "branch_hours_update_authorized",
    },
    "public.branch_special_hours": {
        "branch_special_hours_read_active_member",
        "branch_special_hours_insert_authorized",
        "branch_special_hours_update_authorized",
    },
    "public.branch_hours_projection": {"tenant_isolation_projection"},
    "public.branch_hours_audit_log": {
        "tenant_isolation_audit",
        "internal_branch_hours_audit_insert",
    },
}


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


def _function_contract(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid IS NOT NULL AS function_exists,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                procedure_data.proconfig,
                pg_catalog.pg_get_functiondef(procedure_data.oid) AS function_definition
            FROM (SELECT pg_catalog.to_regprocedure(:signature) AS oid) AS requested
            LEFT JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = requested.oid
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            """
        ),
        {"signature": signature},
    ).mappings().one()


def _trigger_function(bind, trigger_name: str, relation: str) -> str | None:
    schema_name, relation_name = relation.split(".", 1)
    return bind.execute(
        sa.text(
            """
            SELECT procedure_namespace.nspname || '.' || procedure_data.proname || '()'
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = trigger_data.tgfoid
            JOIN pg_catalog.pg_namespace AS procedure_namespace
              ON procedure_namespace.oid = procedure_data.pronamespace
            WHERE relation_namespace.nspname = :schema_name
              AND relation_data.relname = :relation_name
              AND trigger_data.tgname = :trigger_name
              AND NOT trigger_data.tgisinternal
            """
        ),
        {
            "schema_name": schema_name,
            "relation_name": relation_name,
            "trigger_name": trigger_name,
        },
    ).scalar_one_or_none()


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
        raise RuntimeError("b4c5 branch-hours migration requires migration_owner")
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
            WHERE rolname IN (:runtime_role, :security_owner)
            """
        ),
        {"runtime_role": _RUNTIME, "security_owner": _SECURITY_OWNER},
    ).mappings().all()
    by_name = {row["rolname"]: row for row in rows}
    if set(by_name) != {_RUNTIME, _SECURITY_OWNER}:
        raise RuntimeError("required branch-hours roles are missing")
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

    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')"
        ),
        {"role_name": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner")


def _require_relations(bind) -> None:
    relations = tuple(_RUNTIME_PRIVILEGES) + ("public.branch_hours_audit_log",)
    for relation in relations:
        row = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_userbyid(relowner)::text AS owner_name,
                    relrowsecurity,
                    relforcerowsecurity
                FROM pg_catalog.pg_class
                WHERE oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(f"required branch-hours relation is missing: {relation}")
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"unexpected owner for {relation}: {row['owner_name']!r}"
            )
        if not row["relrowsecurity"] or not row["relforcerowsecurity"]:
            raise RuntimeError(f"{relation} must retain ENABLE + FORCE RLS")


def _require_predecessor(bind) -> None:
    _require_identity_and_roles(bind)
    _require_relations(bind)

    for relation in _RUNTIME_PRIVILEGES:
        observed = _direct_privileges(bind, _RUNTIME, relation)
        if observed:
            raise RuntimeError(
                "b4c5 refuses to adopt pre-existing direct app_runtime ACL on "
                f"{relation}: {sorted(observed)!r}"
            )

    for relation, expected in _PREDECESSOR_POLICIES.items():
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"branch-hours predecessor policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    legacy = _function_contract(bind, _LEGACY_AUDIT_SIGNATURE)
    forward = _function_contract(bind, _AUDIT_RUNTIME_SIGNATURE)
    if not legacy["function_exists"]:
        raise RuntimeError("legacy app_private.audit_branch_hours() is missing")
    if legacy["owner_name"] != _MIGRATION_OWNER or legacy["security_definer"]:
        raise RuntimeError("legacy audit trigger function ownership/security drifted")
    if "INSERT INTO public.branch_hours_audit_log" not in legacy["function_definition"]:
        raise RuntimeError("legacy audit trigger function body drifted")
    if forward["function_exists"]:
        raise RuntimeError("forward branch-hours audit function already exists")

    for trigger_name, relation in (
        ("trg_audit_branch_operating_hours", "public.branch_operating_hours"),
        ("trg_audit_branch_special_hours", "public.branch_special_hours"),
    ):
        if _trigger_function(bind, trigger_name, relation) != _LEGACY_AUDIT_SIGNATURE:
            raise RuntimeError(f"legacy audit trigger drifted: {trigger_name}")

    audit_insert_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'branch_hours_audit_log'
                  AND grantee = :role_name
                  AND privilege_type = 'INSERT'
                """
            ),
            {"role_name": _SECURITY_OWNER},
        ).scalars().all()
    )
    if audit_insert_columns:
        raise RuntimeError(
            "b4c5 refuses to adopt pre-existing app_security_owner audit INSERT columns: "
            f"{sorted(audit_insert_columns)!r}"
        )
    if "internal_branch_hours_audit_insert" in _policy_names(
        bind, "public.branch_hours_audit_log"
    ):
        raise RuntimeError("branch-hours internal audit policy already exists")


def _drop_predecessor_policies() -> None:
    op.execute(
        "DROP POLICY tenant_isolation_org_hours ON public.organization_operating_hours"
    )
    op.execute(
        "DROP POLICY tenant_isolation_read_hours ON public.branch_operating_hours"
    )
    op.execute(
        "DROP POLICY write_branch_hours_org_admin ON public.branch_operating_hours"
    )
    op.execute(
        "DROP POLICY write_branch_hours_manager ON public.branch_operating_hours"
    )


def _active_member_expr(target_org: str) -> str:
    return f"""
        EXISTS (
            SELECT 1
            FROM public.organization_members AS member_data
            WHERE member_data.org_id = {target_org}
              AND member_data.org_id = CASE
                    WHEN pg_catalog.pg_input_is_valid(
                        NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                        'uuid'
                    )
                    THEN CAST(NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid)
                    ELSE CAST(NULL AS uuid)
                  END
              AND member_data.user_id = CASE
                    WHEN pg_catalog.pg_input_is_valid(
                        NULLIF(pg_catalog.current_setting('app.current_user_id', true), ''),
                        'uuid'
                    )
                    THEN CAST(NULLIF(pg_catalog.current_setting('app.current_user_id', true), '') AS uuid)
                    ELSE CAST(NULL AS uuid)
                  END
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
        )
    """


def _branch_member_expr(branch_column: str, *, write: bool) -> str:
    role_clause = "TRUE"
    if write:
        role_clause = """
            (
                NULLIF(pg_catalog.current_setting('app.current_role', true), '')
                    IN ('owner', 'admin')
                OR EXISTS (
                    SELECT 1
                    FROM public.branch_staff_roles AS role_assignment
                    WHERE role_assignment.org_id = branch_data.org_id
                      AND role_assignment.branch_id = branch_data.id
                      AND role_assignment.organization_member_id = member_data.id
                      AND role_assignment.role_id = 3
                      AND role_assignment.revoked_at IS NULL
                      AND role_assignment.deleted_at IS NULL
                      AND (
                            role_assignment.effective_from IS NULL
                            OR role_assignment.effective_from <= pg_catalog.clock_timestamp()
                      )
                      AND (
                            role_assignment.effective_to IS NULL
                            OR role_assignment.effective_to > pg_catalog.clock_timestamp()
                      )
                )
            )
        """
    return f"""
        EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            JOIN public.organization_members AS member_data
              ON member_data.org_id = branch_data.org_id
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = CASE
                    WHEN pg_catalog.pg_input_is_valid(
                        NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                        'uuid'
                    )
                    THEN CAST(NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid)
                    ELSE CAST(NULL AS uuid)
                  END
              AND member_data.user_id = CASE
                    WHEN pg_catalog.pg_input_is_valid(
                        NULLIF(pg_catalog.current_setting('app.current_user_id', true), ''),
                        'uuid'
                    )
                    THEN CAST(NULLIF(pg_catalog.current_setting('app.current_user_id', true), '') AS uuid)
                    ELSE CAST(NULL AS uuid)
                  END
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active = TRUE
              AND {role_clause}
        )
    """


def _create_forward_policies() -> None:
    org_member = _active_member_expr("organization_operating_hours.org_id")
    org_write = f"""
        {org_member}
        AND NULLIF(pg_catalog.current_setting('app.current_role', true), '')
            IN ('owner', 'admin')
    """
    op.execute(
        f"""
        CREATE POLICY org_hours_read_active_member
        ON public.organization_operating_hours
        FOR SELECT TO app_runtime
        USING ({org_member})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_insert_owner_admin
        ON public.organization_operating_hours
        FOR INSERT TO app_runtime
        WITH CHECK ({org_write})
        """
    )
    op.execute(
        f"""
        CREATE POLICY org_hours_update_owner_admin
        ON public.organization_operating_hours
        FOR UPDATE TO app_runtime
        USING ({org_write})
        WITH CHECK ({org_write})
        """
    )

    branch_read = _branch_member_expr(
        "branch_operating_hours.branch_id", write=False
    )
    branch_write = _branch_member_expr(
        "branch_operating_hours.branch_id", write=True
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_read_active_member
        ON public.branch_operating_hours
        FOR SELECT TO app_runtime
        USING ({branch_read})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_insert_authorized
        ON public.branch_operating_hours
        FOR INSERT TO app_runtime
        WITH CHECK ({branch_write})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_update_authorized
        ON public.branch_operating_hours
        FOR UPDATE TO app_runtime
        USING ({branch_write})
        WITH CHECK ({branch_write})
        """
    )

    special_read = _branch_member_expr(
        "branch_special_hours.branch_id", write=False
    )
    special_write = _branch_member_expr(
        "branch_special_hours.branch_id", write=True
    )
    op.execute(
        f"""
        CREATE POLICY branch_special_hours_read_active_member
        ON public.branch_special_hours
        FOR SELECT TO app_runtime
        USING ({special_read})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_special_hours_insert_authorized
        ON public.branch_special_hours
        FOR INSERT TO app_runtime
        WITH CHECK ({special_write})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_special_hours_update_authorized
        ON public.branch_special_hours
        FOR UPDATE TO app_runtime
        USING ({special_write})
        WITH CHECK ({special_write})
        """
    )


def _create_internal_audit_boundary() -> None:
    op.execute(
        """
        GRANT INSERT (table_name, record_id, branch_id, operation, changed_by, old_data, new_data)
        ON TABLE public.branch_hours_audit_log
        TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE POLICY internal_branch_hours_audit_insert
        ON public.branch_hours_audit_log
        FOR INSERT TO app_security_owner
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = branch_hours_audit_log.branch_id
                  AND branch_data.org_id = CASE
                        WHEN pg_catalog.pg_input_is_valid(
                            NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                            'uuid'
                        )
                        THEN CAST(NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid)
                        ELSE CAST(NULL AS uuid)
                      END
            )
        )
        """
    )

    op.execute("DROP TRIGGER trg_audit_branch_operating_hours ON public.branch_operating_hours")
    op.execute("DROP TRIGGER trg_audit_branch_special_hours ON public.branch_special_hours")

    op.execute(
        """
        CREATE FUNCTION public.audit_branch_hours_runtime()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            context_org_text text;
            context_user_text text;
            context_org uuid;
            context_user uuid;
            target_branch uuid;
        BEGIN
            context_org_text := NULLIF(
                pg_catalog.current_setting('app.current_org_id', true),
                ''
            );
            context_user_text := NULLIF(
                pg_catalog.current_setting('app.current_user_id', true),
                ''
            );
            IF context_org_text IS NULL
               OR NOT pg_catalog.pg_input_is_valid(context_org_text, 'uuid')
               OR context_user_text IS NULL
               OR NOT pg_catalog.pg_input_is_valid(context_user_text, 'uuid') THEN
                RAISE EXCEPTION 'Branch-hours audit requires typed tenant and actor context'
                    USING ERRCODE = '42501';
            END IF;

            context_org := context_org_text::uuid;
            context_user := context_user_text::uuid;
            target_branch := COALESCE(NEW.branch_id, OLD.branch_id);

            IF NOT EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = target_branch
                  AND branch_data.org_id = context_org
            ) THEN
                RAISE EXCEPTION 'Branch-hours audit tenant mismatch'
                    USING ERRCODE = '42501';
            END IF;

            INSERT INTO public.branch_hours_audit_log (
                table_name,
                record_id,
                branch_id,
                operation,
                changed_by,
                old_data,
                new_data
            )
            VALUES (
                TG_TABLE_NAME,
                COALESCE(NEW.id, OLD.id),
                target_branch,
                TG_OP,
                context_user,
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE pg_catalog.to_jsonb(OLD) END,
                CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE pg_catalog.to_jsonb(NEW) END
            );

            RETURN COALESCE(NEW, OLD);
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_audit_branch_operating_hours
        AFTER INSERT OR UPDATE OR DELETE ON public.branch_operating_hours
        FOR EACH ROW EXECUTE FUNCTION public.audit_branch_hours_runtime()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_branch_special_hours
        AFTER INSERT OR UPDATE OR DELETE ON public.branch_special_hours
        FOR EACH ROW EXECUTE FUNCTION public.audit_branch_hours_runtime()
        """
    )

    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.audit_branch_hours_runtime() OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")
    op.execute(
        "REVOKE ALL ON FUNCTION public.audit_branch_hours_runtime() FROM PUBLIC"
    )


def _grant_runtime_acl() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.organization_operating_hours TO app_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.branch_operating_hours TO app_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.branch_special_hours TO app_runtime"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.branch_hours_projection TO app_runtime"
    )


def _verify_forward(bind) -> None:
    _require_identity_and_roles(bind)
    _require_relations(bind)

    for relation, expected in _RUNTIME_PRIVILEGES.items():
        observed = _direct_privileges(bind, _RUNTIME, relation)
        if observed != expected:
            raise RuntimeError(
                f"branch-hours runtime ACL drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )
        if observed & _FORBIDDEN:
            raise RuntimeError(f"forbidden runtime privilege leaked on {relation}")

    if _direct_privileges(bind, _RUNTIME, "public.branch_hours_audit_log"):
        raise RuntimeError("app_runtime must have zero direct branch-hours audit ACL")

    for relation, expected in _FORWARD_POLICIES.items():
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"branch-hours forward policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    function_row = _function_contract(bind, _AUDIT_RUNTIME_SIGNATURE)
    if (
        not function_row["function_exists"]
        or function_row["owner_name"] != _SECURITY_OWNER
        or not function_row["security_definer"]
    ):
        raise RuntimeError("branch-hours audit function owner/security contract failed")
    definition = function_row["function_definition"] or ""
    for token in (
        "SET row_security TO 'on'",
        "app.current_org_id",
        "app.current_user_id",
        "INSERT INTO public.branch_hours_audit_log",
        "Branch-hours audit tenant mismatch",
    ):
        if token not in definition:
            raise RuntimeError(
                f"branch-hours audit function lost required token {token!r}"
            )

    for trigger_name, relation in (
        ("trg_audit_branch_operating_hours", "public.branch_operating_hours"),
        ("trg_audit_branch_special_hours", "public.branch_special_hours"),
    ):
        if _trigger_function(bind, trigger_name, relation) != _AUDIT_RUNTIME_SIGNATURE:
            raise RuntimeError(f"forward audit trigger drifted: {trigger_name}")

    insert_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'branch_hours_audit_log'
                  AND grantee = :role_name
                  AND privilege_type = 'INSERT'
                """
            ),
            {"role_name": _SECURITY_OWNER},
        ).scalars().all()
    )
    expected_columns = {
        "table_name",
        "record_id",
        "branch_id",
        "operation",
        "changed_by",
        "old_data",
        "new_data",
    }
    if insert_columns != expected_columns:
        raise RuntimeError(
            "branch-hours audit column ACL drift: "
            f"observed={sorted(insert_columns)!r}, expected={sorted(expected_columns)!r}"
        )
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("temporary app_security_owner schema CREATE leaked")


def _drop_forward_objects() -> None:
    op.execute("DROP TRIGGER trg_audit_branch_operating_hours ON public.branch_operating_hours")
    op.execute("DROP TRIGGER trg_audit_branch_special_hours ON public.branch_special_hours")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("DROP FUNCTION public.audit_branch_hours_runtime()")
    op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY internal_branch_hours_audit_insert ON public.branch_hours_audit_log"
    )
    op.execute(
        """
        REVOKE INSERT (table_name, record_id, branch_id, operation, changed_by, old_data, new_data)
        ON TABLE public.branch_hours_audit_log
        FROM app_security_owner
        """
    )

    for relation, policies in (
        (
            "public.organization_operating_hours",
            (
                "org_hours_read_active_member",
                "org_hours_insert_owner_admin",
                "org_hours_update_owner_admin",
            ),
        ),
        (
            "public.branch_operating_hours",
            (
                "branch_hours_read_active_member",
                "branch_hours_insert_authorized",
                "branch_hours_update_authorized",
            ),
        ),
        (
            "public.branch_special_hours",
            (
                "branch_special_hours_read_active_member",
                "branch_special_hours_insert_authorized",
                "branch_special_hours_update_authorized",
            ),
        ),
    ):
        for policy_name in policies:
            op.execute(f"DROP POLICY {policy_name} ON {relation}")

    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON TABLE public.organization_operating_hours FROM app_runtime"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON TABLE public.branch_operating_hours FROM app_runtime"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON TABLE public.branch_special_hours FROM app_runtime"
    )
    op.execute(
        "REVOKE SELECT ON TABLE public.branch_hours_projection FROM app_runtime"
    )


def _restore_predecessor_policies() -> None:
    op.execute(
        """
        CREATE POLICY tenant_isolation_org_hours
        ON public.organization_operating_hours
        FOR ALL
        USING (
            EXISTS (
                SELECT 1
                FROM public.organization_members om
                WHERE om.org_id = organization_operating_hours.org_id
                  AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
                  AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                  AND om.deleted_at IS NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_read_hours
        ON public.branch_operating_hours
        FOR SELECT
        USING (
            EXISTS (
                SELECT 1
                FROM public.org_branches b
                JOIN public.organization_members om ON om.org_id = b.org_id
                JOIN public.org_branch_state obs ON b.id = obs.branch_id
                WHERE b.id = branch_operating_hours.branch_id
                  AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
                  AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                  AND om.deleted_at IS NULL
                  AND obs.deleted_at IS NULL
                  AND obs.is_active = TRUE
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY write_branch_hours_org_admin
        ON public.branch_operating_hours
        FOR ALL
        USING (
            EXISTS (
                SELECT 1
                FROM public.organization_members om
                JOIN public.org_branches b ON b.org_id = om.org_id
                JOIN public.org_branch_state obs ON b.id = obs.branch_id
                WHERE b.id = branch_operating_hours.branch_id
                  AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
                  AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                  AND om.membership_status_id = app_private.membership_status_id('org_admin')
                  AND om.deleted_at IS NULL
                  AND obs.deleted_at IS NULL
                  AND obs.is_active = TRUE
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY write_branch_hours_manager
        ON public.branch_operating_hours
        FOR ALL
        USING (
            EXISTS (
                SELECT 1
                FROM public.org_branches b
                JOIN public.branch_staff_roles bsr ON bsr.branch_id = b.id
                JOIN public.organization_members om ON om.id = bsr.organization_member_id
                JOIN public.org_branch_state obs ON b.id = obs.branch_id
                WHERE b.id = branch_operating_hours.branch_id
                  AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
                  AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                  AND bsr.role_id = app_private.role_id('manager')
                  AND bsr.revoked_at IS NULL
                  AND obs.deleted_at IS NULL
                  AND obs.is_active = TRUE
            )
        )
        """
    )


def _restore_predecessor_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_audit_branch_operating_hours
        AFTER INSERT OR UPDATE OR DELETE ON public.branch_operating_hours
        FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_branch_special_hours
        AFTER INSERT OR UPDATE OR DELETE ON public.branch_special_hours
        FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours()
        """
    )


def _verify_predecessor(bind) -> None:
    for relation in _RUNTIME_PRIVILEGES:
        if _direct_privileges(bind, _RUNTIME, relation):
            raise RuntimeError(f"b4c5 downgrade leaked runtime ACL on {relation}")
    if _direct_privileges(bind, _RUNTIME, "public.branch_hours_audit_log"):
        raise RuntimeError("b4c5 downgrade leaked runtime audit ACL")

    for relation, expected in _PREDECESSOR_POLICIES.items():
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"b4c5 downgrade policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    if _function_contract(bind, _AUDIT_RUNTIME_SIGNATURE)["function_exists"]:
        raise RuntimeError("b4c5 downgrade leaked forward audit function")
    for trigger_name, relation in (
        ("trg_audit_branch_operating_hours", "public.branch_operating_hours"),
        ("trg_audit_branch_special_hours", "public.branch_special_hours"),
    ):
        if _trigger_function(bind, trigger_name, relation) != _LEGACY_AUDIT_SIGNATURE:
            raise RuntimeError(f"b4c5 downgrade failed to restore {trigger_name}")

    insert_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT column_name::text
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'branch_hours_audit_log'
                  AND grantee = :role_name
                  AND privilege_type = 'INSERT'
                """
            ),
            {"role_name": _SECURITY_OWNER},
        ).scalars().all()
    )
    if insert_columns:
        raise RuntimeError("b4c5 downgrade leaked audit INSERT columns")


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    _drop_predecessor_policies()
    _create_forward_policies()
    _create_internal_audit_boundary()
    _grant_runtime_acl()
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_and_roles(bind)
    _verify_forward(bind)
    _drop_forward_objects()
    _restore_predecessor_policies()
    _restore_predecessor_triggers()
    _verify_predecessor(bind)
