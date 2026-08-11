"""Align branch-hours RLS with the application's typed principal domains.

Revision ID: d6e7f8091a2b
Revises: c5d6e7f8091a
Create Date: 2026-08-11

The branch-hours runtime boundary initially treated every authenticated actor as
an ``organization_member``. That is not the application's identity model. The
owner-authentication flow uses ``public.owners``; modern RBAC users use
``organization_users``/``organization_members``; and legacy staff remain in
``gym_owners``. The typed audit-principal registry deliberately preserves these
namespaces.

Ordinary app_runtime must not receive SELECT on credential/staff registries just
so an RLS predicate can validate the caller. This revision therefore keeps the
modern organization-member path under ordinary tenant RLS and validates the
owner/legacy-staff namespaces through a current-session-only SECURITY DEFINER
boolean function owned by the no-login ``app_security_owner``. The helper has a
fixed search_path, row_security=on, derives identity exclusively from the
current ``app.*`` GUCs, and receives only the source columns needed to validate
that principal. PUBLIC cannot execute it and app_runtime receives no direct
registry SELECT capability.

Organization-default writes are limited to validated owner/admin identities.
Branch writes allow those same org-level identities or an active modern RBAC
member with an active branch-manager assignment. Reads require a validated
active principal and same-tenant active branch state.

Downgrade restores the exact b4c5 member-only policy semantics and removes only
the grants/function owned by this revision.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8091a2b"
down_revision = "c5d6e7f8091a"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RUNTIME = "app_runtime"
_SECURITY_OWNER = "app_security_owner"
_VALIDATOR_SIGNATURE = "public.branch_hours_current_nonmember_principal_valid(uuid)"

_APPLICATION_POLICIES = {
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
}

_REQUIRED_OTHER_POLICIES = {
    "public.branch_operating_hours": {
        "internal_branch_hours_soft_delete_update",
    },
    "public.branch_special_hours": {
        "internal_branch_special_hours_soft_delete_update",
    },
    "public.branch_hours_projection": {
        "internal_branch_hours_projection_delete",
        "tenant_isolation_projection",
    },
    "public.branch_hours_audit_log": {
        "internal_branch_hours_audit_insert",
        "tenant_isolation_audit",
    },
}


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


def _column_privilege(
    bind,
    *,
    table_name: str,
    column_name: str,
    grantee: str,
    privilege: str = "SELECT",
) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.column_privileges
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND column_name = :column_name
                      AND grantee = :grantee
                      AND privilege_type = :privilege
                )
                """
            ),
            {
                "table_name": table_name,
                "column_name": column_name,
                "grantee": grantee,
                "privilege": privilege,
            },
        ).scalar_one()
    )


def _require_preflight(bind) -> None:
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
        raise RuntimeError("d6e7 typed-principal migration requires migration_owner")
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

    security_owner = bind.execute(
        sa.text(
            """
            SELECT
                role_data.rolname IS NOT NULL AS role_exists,
                role_data.rolcanlogin,
                role_data.rolsuper,
                role_data.rolbypassrls,
                pg_catalog.pg_has_role(
                    session_user,
                    CAST(:role_name AS name),
                    'SET'
                ) AS migration_owner_can_set,
                pg_catalog.has_schema_privilege(
                    CAST(:role_name AS name), 'public', 'USAGE'
                ) AS has_public_usage,
                pg_catalog.has_schema_privilege(
                    CAST(:role_name AS name), 'public', 'CREATE'
                ) AS has_public_create
            FROM (SELECT 1) AS singleton
            LEFT JOIN pg_catalog.pg_roles AS role_data
              ON role_data.rolname = :role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).mappings().one()
    if not security_owner["role_exists"]:
        raise RuntimeError("d6e7 requires managed role app_security_owner")
    if (
        security_owner["rolcanlogin"]
        or security_owner["rolsuper"]
        or security_owner["rolbypassrls"]
    ):
        raise RuntimeError(
            "app_security_owner must remain NOLOGIN/NOSUPERUSER/NOBYPASSRLS"
        )
    if not security_owner["migration_owner_can_set"]:
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner")
    if not security_owner["has_public_usage"]:
        raise RuntimeError("app_security_owner lacks required public USAGE")
    if security_owner["has_public_create"]:
        raise RuntimeError(
            "d6e7 refuses pre-existing CREATE on public for app_security_owner"
        )

    required = (
        "public.owners",
        "public.organization_members",
        "public.gym_owners",
        "public.org_branches",
        "public.org_branch_state",
        "public.branch_staff_roles",
        *tuple(_APPLICATION_POLICIES),
        *tuple(_REQUIRED_OTHER_POLICIES),
    )
    missing = bind.execute(
        sa.text(
            """
            SELECT relation_name
            FROM unnest(CAST(:relations AS text[])) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY relation_name
            """
        ),
        {"relations": sorted(set(required))},
    ).scalars().all()
    if missing:
        raise RuntimeError(
            f"d6e7 required relations are missing: {tuple(missing)!r}"
        )

    if bind.execute(
        sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
        {"signature": _VALIDATOR_SIGNATURE},
    ).scalar_one():
        raise RuntimeError("d6e7 principal validator already exists")

    for relation in ("public.owners", "public.gym_owners"):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role, :relation, 'SELECT')"
            ),
            {"role": _RUNTIME, "relation": relation},
        ).scalar_one():
            raise RuntimeError(
                f"d6e7 refuses a predecessor with app_runtime SELECT on {relation}"
            )

    # 8192 owns id/org_id/role SELECT on gym_owners for its legacy RBAC guard.
    for column_name in ("id", "org_id", "role"):
        if not _column_privilege(
            bind,
            table_name="gym_owners",
            column_name=column_name,
            grantee=_SECURITY_OWNER,
        ):
            raise RuntimeError(
                "d6e7 requires the 8192 bounded gym_owners identity columns"
            )

    for table_name, columns in (
        ("owners", ("id", "org_id", "email_verified", "onboarding_completed")),
        ("gym_owners", ("is_active", "is_verified")),
    ):
        for column_name in columns:
            if _column_privilege(
                bind,
                table_name=table_name,
                column_name=column_name,
                grantee=_SECURITY_OWNER,
            ):
                raise RuntimeError(
                    "d6e7 refuses to adopt pre-existing validation column grant: "
                    f"{table_name}.{column_name}"
                )

    for relation, application_names in _APPLICATION_POLICIES.items():
        expected = set(application_names) | _REQUIRED_OTHER_POLICIES.get(relation, set())
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 predecessor policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    for relation, expected in _REQUIRED_OTHER_POLICIES.items():
        if relation in _APPLICATION_POLICIES:
            continue
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 protected policy drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )


def _uuid_guc(name: str) -> str:
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


def _create_nonmember_validator() -> None:
    op.execute(
        """
        GRANT SELECT (id, org_id, email_verified, onboarding_completed)
        ON TABLE public.owners
        TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT (is_active, is_verified)
        ON TABLE public.gym_owners
        TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.branch_hours_current_nonmember_principal_valid(
            p_org_id uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_principal_type text;
            v_role text;
            v_user_id uuid;
            v_context_org uuid;
        BEGIN
            v_principal_type := NULLIF(
                pg_catalog.current_setting('app.current_principal_type', true), ''
            );
            v_role := NULLIF(
                pg_catalog.current_setting('app.current_role', true), ''
            );

            IF NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_user_id', true), ''),
                'uuid'
            ) OR NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            ) THEN
                RETURN FALSE;
            END IF;

            v_user_id := CAST(
                NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')
                AS uuid
            );
            v_context_org := CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')
                AS uuid
            );

            IF p_org_id IS NULL OR p_org_id IS DISTINCT FROM v_context_org THEN
                RETURN FALSE;
            END IF;

            IF v_principal_type = 'owner' THEN
                IF v_role IS DISTINCT FROM 'owner' THEN
                    RETURN FALSE;
                END IF;
                RETURN EXISTS (
                    SELECT 1
                    FROM public.owners AS owner_data
                    WHERE owner_data.id = v_user_id
                      AND owner_data.org_id = p_org_id
                      AND owner_data.email_verified IS TRUE
                      AND owner_data.onboarding_completed IS TRUE
                );
            END IF;

            IF v_principal_type = 'legacy_gym_owner' THEN
                RETURN EXISTS (
                    SELECT 1
                    FROM public.gym_owners AS staff_data
                    WHERE staff_data.id = v_user_id
                      AND staff_data.org_id = p_org_id
                      AND staff_data.is_active IS TRUE
                      AND staff_data.is_verified IS TRUE
                      AND staff_data.role::text = v_role
                );
            END IF;

            RETURN FALSE;
        END;
        $function$;
        """
    )

    # ALTER OWNER requires temporary CREATE for the target owner on the
    # containing schema. Keep the privilege window transaction-local in effect
    # and restore the standing no-CREATE boundary immediately afterward.
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        """
        ALTER FUNCTION public.branch_hours_current_nonmember_principal_valid(uuid)
        OWNER TO app_security_owner
        """
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")
    op.execute(
        """
        REVOKE ALL ON FUNCTION
        public.branch_hours_current_nonmember_principal_valid(uuid)
        FROM PUBLIC
        """
    )
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION
        public.branch_hours_current_nonmember_principal_valid(uuid)
        TO app_runtime
        """
    )


def _active_owner_expr(target_org: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'owner'
            AND NULLIF(pg_catalog.current_setting('app.current_role', true), '') = 'owner'
            AND {target_org} = {current_org}
            AND public.branch_hours_current_nonmember_principal_valid({target_org})
        )
    """


def _active_org_user_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'organization_user'
            AND {target_org} = {current_org}
            AND EXISTS (
                SELECT 1
                FROM public.organization_members AS member_data
                WHERE member_data.org_id = {target_org}
                  AND member_data.user_id = {current_user}
                  AND member_data.membership_status_id = 3
                  AND member_data.deleted_at IS NULL
            )
        )
    """


def _active_legacy_staff_expr(target_org: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'legacy_gym_owner'
            AND {target_org} = {current_org}
            AND public.branch_hours_current_nonmember_principal_valid({target_org})
        )
    """


def _active_principal_expr(target_org: str) -> str:
    return f"""
        (
            {_active_owner_expr(target_org)}
            OR {_active_org_user_expr(target_org)}
            OR {_active_legacy_staff_expr(target_org)}
        )
    """


def _org_write_expr(target_org: str) -> str:
    owner = _active_owner_expr(target_org)
    legacy_admin = f"""
        (
            {_active_legacy_staff_expr(target_org)}
            AND NULLIF(pg_catalog.current_setting('app.current_role', true), '')
                IN ('owner', 'admin')
        )
    """
    # Modern organization-user org-admin semantics are not yet represented by a
    # canonical org-scoped role relation. Do not authorize from a role claim
    # alone. Modern managers remain eligible for scoped branch writes below.
    return f"({owner} OR {legacy_admin})"


def _branch_read_expr(branch_column: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            JOIN public.org_branch_state AS branch_state
              ON branch_state.branch_id = branch_data.id
             AND branch_state.org_id = branch_data.org_id
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = {current_org}
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active IS TRUE
              AND {_active_principal_expr('branch_data.org_id')}
        )
    """


def _branch_write_expr(branch_column: str) -> str:
    current_org = _uuid_guc("app.current_org_id")
    current_user = _uuid_guc("app.current_user_id")
    org_level = _org_write_expr("branch_data.org_id")
    modern_manager = f"""
        (
            NULLIF(pg_catalog.current_setting('app.current_principal_type', true), '') = 'organization_user'
            AND EXISTS (
                SELECT 1
                FROM public.organization_members AS member_data
                JOIN public.branch_staff_roles AS role_assignment
                  ON role_assignment.organization_member_id = member_data.id
                 AND role_assignment.org_id = member_data.org_id
                WHERE member_data.org_id = branch_data.org_id
                  AND member_data.user_id = {current_user}
                  AND member_data.membership_status_id = 3
                  AND member_data.deleted_at IS NULL
                  AND role_assignment.branch_id = branch_data.id
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
            WHERE branch_data.id = {branch_column}
              AND branch_data.org_id = {current_org}
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active IS TRUE
              AND ({org_level} OR {modern_manager})
        )
    """


def _drop_application_policies() -> None:
    for relation, policy_names in _APPLICATION_POLICIES.items():
        for policy_name in sorted(policy_names):
            op.execute(f"DROP POLICY {policy_name} ON {relation}")


def _create_typed_policies() -> None:
    org_read = _active_principal_expr("organization_operating_hours.org_id")
    org_write = _org_write_expr("organization_operating_hours.org_id")

    op.execute(
        f"""
        CREATE POLICY org_hours_read_active_member
        ON public.organization_operating_hours
        FOR SELECT TO app_runtime
        USING ({org_read})
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

    for relation, prefix in (
        ("public.branch_operating_hours", "branch_hours"),
        ("public.branch_special_hours", "branch_special_hours"),
    ):
        branch_column = f"{relation.split('.', 1)[1]}.branch_id"
        read_expr = _branch_read_expr(branch_column)
        write_expr = _branch_write_expr(branch_column)
        op.execute(
            f"""
            CREATE POLICY {prefix}_read_active_member
            ON {relation}
            FOR SELECT TO app_runtime
            USING ({read_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_insert_authorized
            ON {relation}
            FOR INSERT TO app_runtime
            WITH CHECK ({write_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_update_authorized
            ON {relation}
            FOR UPDATE TO app_runtime
            USING ({write_expr})
            WITH CHECK ({write_expr})
            """
        )


def _b4_active_member_expr(target_org: str) -> str:
    current_user = _uuid_guc("app.current_user_id")
    current_org = _uuid_guc("app.current_org_id")
    return f"""
        EXISTS (
            SELECT 1
            FROM public.organization_members AS member_data
            WHERE member_data.org_id = {target_org}
              AND member_data.org_id = {current_org}
              AND member_data.user_id = {current_user}
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
        )
    """


def _b4_branch_member_expr(branch_column: str, *, write: bool) -> str:
    current_org = _uuid_guc("app.current_org_id")
    current_user = _uuid_guc("app.current_user_id")
    role_clause = "TRUE"
    if write:
        role_clause = f"""
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
              AND branch_data.org_id = {current_org}
              AND member_data.user_id = {current_user}
              AND member_data.membership_status_id = 3
              AND member_data.deleted_at IS NULL
              AND branch_state.deleted_at IS NULL
              AND branch_state.is_active = TRUE
              AND {role_clause}
        )
    """


def _restore_b4_policies() -> None:
    org_member = _b4_active_member_expr("organization_operating_hours.org_id")
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

    for relation, prefix in (
        ("public.branch_operating_hours", "branch_hours"),
        ("public.branch_special_hours", "branch_special_hours"),
    ):
        branch_column = f"{relation.split('.', 1)[1]}.branch_id"
        read_expr = _b4_branch_member_expr(branch_column, write=False)
        write_expr = _b4_branch_member_expr(branch_column, write=True)
        op.execute(
            f"""
            CREATE POLICY {prefix}_read_active_member
            ON {relation}
            FOR SELECT TO app_runtime
            USING ({read_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_insert_authorized
            ON {relation}
            FOR INSERT TO app_runtime
            WITH CHECK ({write_expr})
            """
        )
        op.execute(
            f"""
            CREATE POLICY {prefix}_update_authorized
            ON {relation}
            FOR UPDATE TO app_runtime
            USING ({write_expr})
            WITH CHECK ({write_expr})
            """
        )


def _verify_forward(bind) -> None:
    for relation, application_names in _APPLICATION_POLICIES.items():
        expected = set(application_names) | _REQUIRED_OTHER_POLICIES.get(relation, set())
        observed = _policy_names(bind, relation)
        if observed != expected:
            raise RuntimeError(
                f"d6e7 final policy inventory drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )

    function = bind.execute(
        sa.text(
            """
            SELECT
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                procedure_data.provolatile::text AS volatility,
                procedure_data.proconfig,
                pg_catalog.has_function_privilege(
                    'app_runtime', procedure_data.oid, 'EXECUTE'
                ) AS runtime_execute,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            procedure_data.proacl,
                            pg_catalog.acldefault('f', procedure_data.proowner)
                        )
                    ) AS acl_data
                    WHERE acl_data.grantee = 0
                      AND acl_data.privilege_type = 'EXECUTE'
                ) AS public_execute
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": _VALIDATOR_SIGNATURE},
    ).mappings().one_or_none()
    if function is None:
        raise RuntimeError("d6e7 principal validator is missing")
    if (
        function["owner_name"] != _SECURITY_OWNER
        or not function["security_definer"]
        or function["volatility"] != "s"
        or not function["runtime_execute"]
        or function["public_execute"]
    ):
        raise RuntimeError(f"d6e7 validator contract drifted: {dict(function)!r}")
    settings = set(function["proconfig"] or [])
    if "search_path=pg_catalog, public" not in settings or "row_security=on" not in settings:
        raise RuntimeError(f"d6e7 validator settings drifted: {settings!r}")

    if bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.has_table_privilege('app_runtime', 'public.owners', 'SELECT')
                OR pg_catalog.has_table_privilege(
                    'app_runtime', 'public.gym_owners', 'SELECT'
                )
            """
        )
    ).scalar_one():
        raise RuntimeError("d6e7 leaked source-registry SELECT to app_runtime")

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("d6e7 left app_security_owner with public CREATE")


def _drop_nonmember_validator() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION public.branch_hours_current_nonmember_principal_valid(uuid)"
    )
    op.execute("RESET ROLE")
    op.execute(
        """
        REVOKE SELECT (id, org_id, email_verified, onboarding_completed)
        ON TABLE public.owners
        FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE SELECT (is_active, is_verified)
        ON TABLE public.gym_owners
        FROM app_security_owner
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)
    _create_nonmember_validator()
    _drop_application_policies()
    _create_typed_policies()
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner_only = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    if _require_migration_owner_only != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("d6e7 downgrade requires migration_owner")
    _verify_forward(bind)
    _drop_application_policies()
    _restore_b4_policies()
    _drop_nonmember_validator()