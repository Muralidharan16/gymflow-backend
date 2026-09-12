"""RBAC Hardening Phase 15 to 18

Revision ID: a1b2c3d4e5f6
Revises: 970059a0665d
Create Date: 2026-05-23 16:32:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# A1B2C3D4E5F6_OWNER_CONTEXT_HELPERS_START

_A1_MIGRATION_OWNER = "migration_owner"
_A1_SECURITY_OWNER = "app_security_owner"
_A1_VIEW = "app_secure.v_active_branch_staff_roles"
_A1_FUNCTION = "app_private.ensure_future_partition(text,integer)"
_A1_VIEW_COMMENT = (
    "Tenant-safe security-invoker view of canonical branch staff roles."
)
_A1_PREDECESSOR_COLUMNS = (
    "id", "org_id", "branch_id", "organization_member_id", "role_id",
    "role_code", "hierarchy_level", "scope_type_id", "scope_code",
    "assignment_source", "assigned_by", "assigned_at", "effective_from",
    "effective_to", "user_id", "role_legacy", "created_at",
)
_A1_PREDECESSOR_DEPENDENCIES = (
    "public.branch_staff_roles",
    "public.organization_members",
    "public.scope_types",
    "public.staff_roles",
)
_A1_NEW_INDEXES = {
    "public.ix_roles_active_lookup": (
        "public.branch_staff_roles", "org_id", "branch_id",
        "organization_member_id", "revoked_at is null", "deleted_at is null",
    ),
    "public.ix_member_active": (
        "public.organization_members", "org_id", "user_id",
        "deleted_at is null",
    ),
    "public.ix_snapshot_active": (
        "public.member_permission_snapshots", "organization_member_id",
        "is_stale = false",
    ),
    "public.ix_auth_sessions_active": (
        "public.auth_sessions", "user_id", "org_id", "revoked_at is null",
    ),
}

_A1_FORWARD_VIEW_SQL = """
CREATE VIEW app_secure.v_active_branch_staff_roles
WITH (security_barrier = true, security_invoker = true) AS
SELECT *
FROM public.branch_staff_roles
WHERE deleted_at IS NULL AND revoked_at IS NULL
"""

_A1_PREDECESSOR_VIEW_SQL = """
CREATE VIEW app_secure.v_active_branch_staff_roles
WITH (security_barrier = true, security_invoker = true) AS
SELECT
    bsr.id,
    bsr.org_id,
    bsr.branch_id,
    bsr.organization_member_id,
    bsr.role_id,
    sr.code AS role_code,
    sr.hierarchy_level,
    bsr.scope_type_id,
    st.code AS scope_code,
    bsr.assignment_source,
    bsr.assigned_by,
    bsr.assigned_at,
    bsr.effective_from,
    bsr.effective_to,
    om.user_id AS user_id,
    sr.code AS role_legacy,
    bsr.created_at
FROM public.branch_staff_roles AS bsr
JOIN public.organization_members AS om
  ON om.id = bsr.organization_member_id
 AND om.org_id = bsr.org_id
JOIN public.staff_roles AS sr ON sr.id = bsr.role_id
JOIN public.scope_types AS st ON st.id = bsr.scope_type_id
WHERE bsr.deleted_at IS NULL AND bsr.revoked_at IS NULL
"""

_A1_FORWARD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(
    p_table_name TEXT,
    p_days_ahead INT
)
RETURNS VOID STRICT VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
LANGUAGE plpgsql AS $function$
DECLARE
    v_qualified_name TEXT;
    v_partition_date TIMESTAMPTZ :=
        clock_timestamp() + (p_days_ahead || ' days')::interval;
    v_partition_name TEXT;
    v_start_str TEXT;
    v_end_str TEXT;
BEGIN
    v_qualified_name := CASE p_table_name
        WHEN 'branch_audit_log' THEN 'public.branch_audit_log'
        WHEN 'auth_sessions' THEN 'public.auth_sessions'
        ELSE NULL
    END;
    IF v_qualified_name IS NULL THEN
        RAISE EXCEPTION 'Invalid partition target: %', p_table_name;
    END IF;
    v_partition_name := replace(p_table_name, '.', '_')
                        || '_' || to_char(v_partition_date, 'YYYY_MM');
    v_start_str := to_char(
        date_trunc('month', v_partition_date), 'YYYY-MM-DD'
    );
    v_end_str := to_char(
        date_trunc('month', v_partition_date) + interval '1 month',
        'YYYY-MM-DD'
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF %s '
        'FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_qualified_name, v_start_str, v_end_str
    );
END;
$function$
"""

_A1_PREDECESSOR_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(
    p_table_name TEXT,
    p_days_ahead INT
)
RETURNS VOID STRICT VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
LANGUAGE plpgsql AS $function$
DECLARE
    v_qualified_name TEXT;
    v_partition_date TIMESTAMPTZ :=
        clock_timestamp() + (p_days_ahead || ' days')::interval;
    v_partition_name TEXT;
    v_start_str TEXT;
    v_end_str TEXT;
BEGIN
    v_qualified_name := CASE p_table_name
        WHEN 'branch_audit_log' THEN 'public.branch_audit_log'
        WHEN 'auth_sessions' THEN 'public.auth_sessions'
        ELSE NULL
    END;
    IF v_qualified_name IS NULL THEN
        RAISE EXCEPTION
            'Invalid partition target: %. Allowed: branch_audit_log, auth_sessions.',
            p_table_name
        USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_partition_name := replace(p_table_name, '.', '_')
                        || '_' || to_char(v_partition_date, 'YYYY_MM');
    v_start_str := to_char(
        date_trunc('month', v_partition_date), 'YYYY-MM-DD'
    );
    v_end_str := to_char(
        date_trunc('month', v_partition_date) + interval '1 month',
        'YYYY-MM-DD'
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF %s '
        'FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_qualified_name, v_start_str, v_end_str
    );
END;
$function$
"""


def _a1_bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError(
            "a1b2c3d4e5f6 requires online catalog access; "
            "offline SQL generation is unsupported."
        )
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable.")
    return bind


def _a1_identity(bind):
    return dict(
        bind.execute(
            sa.text(
                "SELECT session_user::text AS session_user_name, "
                "current_user::text AS current_user_name"
            )
        ).mappings().one()
    )


def _a1_require_migration_owner(bind):
    observed = _a1_identity(bind)
    expected = {
        "session_user_name": _A1_MIGRATION_OWNER,
        "current_user_name": _A1_MIGRATION_OWNER,
    }
    if observed != expected:
        raise RuntimeError(
            "a1b2c3d4e5f6 requires "
            "session_user=current_user=migration_owner; "
            f"observed {observed!r}."
        )


def _a1_can_set_security_owner(bind):
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "session_user, 'app_security_owner', 'SET')"
            )
        ).scalar_one()
    )


def _a1_run_as_security_owner(bind, statements):
    _a1_require_migration_owner(bind)
    if not _a1_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    statements = (
        statements
        if isinstance(statements, (tuple, list))
        else (statements,)
    )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    identity = _a1_identity(bind)
    if identity["session_user_name"] != _A1_MIGRATION_OWNER:
        raise RuntimeError("SET LOCAL ROLE changed session_user.")
    if identity["current_user_name"] != _A1_SECURITY_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE did not enter app_security_owner."
        )
    for statement in statements:
        bind.exec_driver_sql(statement)
    # RESET is success-only. If protected SQL aborts the transaction,
    # rollback clears the LOCAL role without masking the original exception.
    bind.execute(sa.text("RESET ROLE"))
    _a1_require_migration_owner(bind)


def _a1_schema_privilege(bind, role_name, privilege):
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role_name, 'app_private', :privilege)"
            ),
            {"role_name": role_name, "privilege": privilege},
        ).scalar_one()
    )


def _a1_direct_private_acl(bind, privilege):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor.rolname::text,
                grantee.rolname::text,
                acl.privilege_type::text,
                acl.is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL
                pg_catalog.aclexplode(namespace.nspacl) AS acl
            JOIN pg_catalog.pg_roles AS grantor
              ON grantor.oid = acl.grantor
            JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'app_private'
              AND grantee.rolname = 'app_security_owner'
              AND acl.privilege_type = :privilege
            ORDER BY 1, 2, 3, 4
            """
        ),
        {"privilege": privilege},
    ).all()
    return tuple((row[0], row[1], row[2], bool(row[3])) for row in rows)


def _a1_public_private_privileges(bind):
    return {
        privilege: bool(
            bind.execute(
                sa.text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_namespace AS namespace
                        CROSS JOIN LATERAL pg_catalog.aclexplode(
                            COALESCE(
                                namespace.nspacl,
                                pg_catalog.acldefault(
                                    'n', namespace.nspowner
                                )
                            )
                        ) AS acl
                        WHERE namespace.nspname = 'app_private'
                          AND acl.grantee = 0
                          AND acl.privilege_type = :privilege
                    )
                    """
                ),
                {"privilege": privilege},
            ).scalar_one()
        )
        for privilege in ("USAGE", "CREATE")
    }


def _a1_prepare_app_private_owner_window(bind):
    _a1_require_migration_owner(bind)
    before = {
        privilege: _a1_direct_private_acl(bind, privilege)
        for privilege in ("USAGE", "CREATE")
    }
    effective_before = {
        privilege: _a1_schema_privilege(
            bind, _A1_SECURITY_OWNER, privilege
        )
        for privilege in ("USAGE", "CREATE")
    }
    public_before = _a1_public_private_privileges(bind)
    if any(public_before.values()):
        raise RuntimeError("PUBLIC authority on app_private is forbidden.")
    added = []
    for privilege in ("USAGE", "CREATE"):
        if effective_before[privilege]:
            continue
        bind.execute(
            sa.text(
                f"GRANT {privilege} ON SCHEMA app_private "
                "TO app_security_owner"
            )
        )
        after = _a1_direct_private_acl(bind, privilege)
        delta = [row for row in after if row not in before[privilege]]
        expected = [
            (
                _A1_MIGRATION_OWNER,
                _A1_SECURITY_OWNER,
                privilege,
                False,
            )
        ]
        if delta != expected:
            raise RuntimeError(
                f"Unexpected temporary app_private {privilege} "
                f"ACL delta: {delta!r}."
            )
        if not _a1_schema_privilege(
            bind, _A1_SECURITY_OWNER, privilege
        ):
            raise RuntimeError(
                f"app_security_owner lacks effective {privilege}."
            )
        added.append(privilege)
    if _a1_public_private_privileges(bind) != public_before:
        raise RuntimeError(
            "PUBLIC app_private authority changed during preparation."
        )
    return {
        "before": before,
        "effective_before": effective_before,
        "public_before": public_before,
        "added": tuple(added),
    }


def _a1_restore_app_private_owner_window(bind, state):
    _a1_require_migration_owner(bind)
    for privilege in reversed(state["added"]):
        bind.execute(
            sa.text(
                f"REVOKE {privilege} ON SCHEMA app_private "
                "FROM app_security_owner"
            )
        )
    for privilege in ("USAGE", "CREATE"):
        observed = _a1_direct_private_acl(bind, privilege)
        if observed != state["before"][privilege]:
            raise RuntimeError(
                f"Exact app_private {privilege} ACL restoration failed: "
                f"observed={observed!r}, "
                f"expected={state['before'][privilege]!r}."
            )
        effective = _a1_schema_privilege(
            bind, _A1_SECURITY_OWNER, privilege
        )
        if effective != state["effective_before"][privilege]:
            raise RuntimeError(
                f"Effective app_private {privilege} authority drifted."
            )
    if _a1_public_private_privileges(bind) != state["public_before"]:
        raise RuntimeError("PUBLIC app_private authority was not restored.")


def _a1_relation_columns(bind, schema_name, relation_name):
    return tuple(
        bind.execute(
            sa.text(
                """
                SELECT attribute.attname::text
                FROM pg_catalog.pg_attribute AS attribute
                JOIN pg_catalog.pg_class AS relation
                  ON relation.oid = attribute.attrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = :schema_name
                  AND relation.relname = :relation_name
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                ORDER BY attribute.attnum
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
            },
        ).scalars().all()
    )


def _a1_view_dependencies(bind, view_oid):
    return tuple(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT
                    referenced_namespace.nspname::text || '.' ||
                    referenced_relation.relname::text AS relation_name
                FROM pg_catalog.pg_rewrite AS rewrite
                JOIN pg_catalog.pg_depend AS dependency
                  ON dependency.classid = 'pg_rewrite'::regclass
                 AND dependency.objid = rewrite.oid
                 AND dependency.refclassid = 'pg_class'::regclass
                JOIN pg_catalog.pg_class AS referenced_relation
                  ON referenced_relation.oid = dependency.refobjid
                JOIN pg_catalog.pg_namespace AS referenced_namespace
                  ON referenced_namespace.oid =
                     referenced_relation.relnamespace
                WHERE rewrite.ev_class = CAST(:view_oid AS oid)
                  AND referenced_relation.oid <> CAST(:view_oid AS oid)
                  AND referenced_relation.relkind IN ('r', 'p', 'v', 'm')
                ORDER BY relation_name
                """
            ),
            {"view_oid": view_oid},
        ).scalars().all()
    )


def _a1_view_acl(bind, view_oid):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                COALESCE(grantee.rolname::text, 'PUBLIC'),
                acl.privilege_type::text,
                acl.is_grantable,
                grantor.rolname::text
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault('r', relation.relowner)
                )
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantor
              ON grantor.oid = acl.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            WHERE relation.oid = CAST(:view_oid AS oid)
              AND acl.grantee <> relation.relowner
            ORDER BY 1, 2, 3, 4
            """
        ),
        {"view_oid": view_oid},
    ).all()
    return tuple((row[0], row[1], bool(row[2]), row[3]) for row in rows)


def _a1_verify_view_contract(bind, generation):
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation.oid::oid AS relation_oid,
                relation.relkind::text AS relation_kind,
                owner.rolname::text AS owner_name,
                COALESCE(
                    relation.reloptions,
                    ARRAY[]::text[]
                ) AS reloptions,
                pg_catalog.obj_description(
                    relation.oid, 'pg_class'
                ) AS comment_text
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_roles AS owner
              ON owner.oid = relation.relowner
            WHERE namespace.nspname = 'app_secure'
              AND relation.relname = 'v_active_branch_staff_roles'
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Required view {_A1_VIEW} is absent.")
    if row["relation_kind"] != "v":
        raise RuntimeError(f"{_A1_VIEW} is not a normal view.")
    if row["owner_name"] != _A1_SECURITY_OWNER:
        raise RuntimeError(
            f"{_A1_VIEW} has unexpected owner {row['owner_name']!r}."
        )
    if set(row["reloptions"]) != {
        "security_barrier=true",
        "security_invoker=true",
    }:
        raise RuntimeError(
            f"{_A1_VIEW} security options drifted: "
            f"{row['reloptions']!r}."
        )
    if row["comment_text"] != _A1_VIEW_COMMENT:
        raise RuntimeError(f"{_A1_VIEW} comment drifted.")
    expected_acl = (
        ("app_runtime", "SELECT", False, _A1_SECURITY_OWNER),
        ("readonly_analytics", "SELECT", False, _A1_SECURITY_OWNER),
    )
    observed_acl = _a1_view_acl(bind, row["relation_oid"])
    if observed_acl != expected_acl:
        raise RuntimeError(f"{_A1_VIEW} ACL drifted: {observed_acl!r}.")
    dependencies = _a1_view_dependencies(bind, row["relation_oid"])
    columns = _a1_relation_columns(
        bind, "app_secure", "v_active_branch_staff_roles"
    )
    if generation == "predecessor":
        if dependencies != _A1_PREDECESSOR_DEPENDENCIES:
            raise RuntimeError(
                "Predecessor view dependencies drifted: "
                f"{dependencies!r}."
            )
        if columns != _A1_PREDECESSOR_COLUMNS:
            raise RuntimeError(
                f"Predecessor view projection drifted: {columns!r}."
            )
    elif generation == "forward":
        expected_columns = _a1_relation_columns(
            bind, "public", "branch_staff_roles"
        )
        if dependencies != ("public.branch_staff_roles",):
            raise RuntimeError(
                "Forward view must depend only on branch_staff_roles; "
                f"observed {dependencies!r}."
            )
        if columns != expected_columns:
            raise RuntimeError(
                f"Forward view projection drifted: {columns!r}."
            )
    else:
        raise RuntimeError(f"Unsupported view generation {generation!r}.")


def _a1_verify_function_contract(bind, generation):
    row = bind.execute(
        sa.text(
            """
            SELECT
                owner.rolname::text AS owner_name,
                procedure.prokind::text AS procedure_kind,
                procedure.prosecdef AS security_definer,
                procedure.proisstrict AS is_strict,
                procedure.provolatile::text AS volatility,
                procedure.proparallel::text AS parallel_safety,
                language.lanname::text AS language_name,
                pg_catalog.format_type(
                    procedure.prorettype, NULL
                )::text AS result_type,
                COALESCE(
                    procedure.proconfig,
                    ARRAY[]::text[]
                ) AS config,
                procedure.prosrc::text AS source_text,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            procedure.proacl,
                            pg_catalog.acldefault(
                                'f', procedure.proowner
                            )
                        )
                    ) AS acl
                    WHERE acl.grantee <> procedure.proowner
                      AND acl.privilege_type = 'EXECUTE'
                ) AS non_owner_execute
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_roles AS owner
              ON owner.oid = procedure.proowner
            JOIN pg_catalog.pg_language AS language
              ON language.oid = procedure.prolang
            WHERE procedure.oid = pg_catalog.to_regprocedure(
                'app_private.ensure_future_partition(text,integer)'
            )
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Required function {_A1_FUNCTION} is absent.")
    expected = {
        "owner_name": _A1_SECURITY_OWNER,
        "procedure_kind": "f",
        "security_definer": True,
        "is_strict": True,
        "volatility": "v",
        "parallel_safety": "u",
        "language_name": "plpgsql",
        "result_type": "void",
        "non_owner_execute": False,
    }
    for key, expected_value in expected.items():
        if row[key] != expected_value:
            raise RuntimeError(
                f"Partition-function {key} drift: "
                f"observed={row[key]!r}, expected={expected_value!r}."
            )
    config = tuple(row["config"])
    source = row["source_text"]
    if generation == "predecessor":
        if config != ("search_path=pg_catalog, public",):
            raise RuntimeError(
                f"Predecessor function config drifted: {config!r}."
            )
        for marker in (
            "Allowed: branch_audit_log, auth_sessions.",
            "invalid_parameter_value",
        ):
            if marker not in source:
                raise RuntimeError(
                    "Predecessor partition-function definition drifted."
                )
    elif generation == "forward":
        if config != ("search_path=pg_catalog",):
            raise RuntimeError(
                f"Forward function config drifted: {config!r}."
            )
        if "invalid_parameter_value" in source:
            raise RuntimeError(
                "Forward function retains predecessor-only error handling."
            )
    else:
        raise RuntimeError(
            f"Unsupported function generation {generation!r}."
        )
    for marker in (
        "public.branch_audit_log",
        "public.auth_sessions",
        "CREATE TABLE IF NOT EXISTS",
    ):
        if marker not in source:
            raise RuntimeError(
                f"Partition-function definition lacks {marker!r}."
            )


def _a1_verify_inherited_rls_and_policy(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                relation.relname::text,
                relation.relrowsecurity,
                relation.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname IN (
                  'branch_staff_roles',
                  'organization_members',
                  'branch_audit_log'
              )
            ORDER BY relation.relname
            """
        )
    ).all()
    observed = tuple(
        (row[0], bool(row[1]), bool(row[2])) for row in rows
    )
    expected = (
        ("branch_audit_log", True, True),
        ("branch_staff_roles", True, True),
        ("organization_members", True, True),
    )
    if observed != expected:
        raise RuntimeError(
            f"Inherited RLS/FORCE contract drifted: {observed!r}."
        )
    policy = bind.execute(
        sa.text(
            """
            SELECT
                policy.polcmd::text,
                policy.polpermissive,
                policy.polroles::oid[],
                pg_catalog.pg_get_expr(
                    policy.polqual, policy.polrelid
                ),
                pg_catalog.pg_get_expr(
                    policy.polwithcheck, policy.polrelid
                )
            FROM pg_catalog.pg_policy AS policy
            WHERE policy.polrelid =
                  'public.branch_staff_roles'::regclass
              AND policy.polname = 'tenant_isolation_staff_roles'
            """
        )
    ).one_or_none()
    if policy is None:
        raise RuntimeError(
            "Inherited tenant_isolation_staff_roles policy is absent."
        )
    using_text = " ".join(policy[3].lower().split())
    check_text = " ".join(policy[4].lower().split())
    if policy[0] != "*" or not policy[1] or tuple(policy[2]) != (0,):
        raise RuntimeError("Inherited staff-role policy shape drifted.")
    for marker in (
        "app.current_org_id",
        "app.can_read_staff_roles",
        "deleted_at is null",
    ):
        if marker not in using_text:
            raise RuntimeError(
                f"Inherited policy USING clause lacks {marker!r}."
            )
    for marker in ("app.current_org_id", "deleted_at is null"):
        if marker not in check_text:
            raise RuntimeError(
                f"Inherited policy WITH CHECK lacks {marker!r}."
            )
    if "app.can_read_staff_roles" in check_text:
        raise RuntimeError(
            "Inherited WITH CHECK contains the read-capability predicate."
        )


def _a1_normalize_index_definition(definition):
    return " ".join(definition.lower().replace('"', "").split())


def _a1_verify_index_contract(bind, *, new_indexes_present):
    audit_definition = bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_get_indexdef("
            "pg_catalog.to_regclass('public.ix_audit_org_sequence'))"
        )
    ).scalar_one_or_none()
    if audit_definition is None:
        raise RuntimeError("Inherited ix_audit_org_sequence is absent.")
    audit_text = _a1_normalize_index_definition(audit_definition)
    for marker in (
        "ix_audit_org_sequence",
        "public.branch_audit_log",
        "org_id",
        "audit_sequence desc",
    ):
        if marker not in audit_text:
            raise RuntimeError(
                "Inherited ix_audit_org_sequence definition drifted."
            )
    for qualified_name, markers in _A1_NEW_INDEXES.items():
        definition = bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_get_indexdef("
                "pg_catalog.to_regclass(:qualified_name))"
            ),
            {"qualified_name": qualified_name},
        ).scalar_one_or_none()
        if new_indexes_present:
            if definition is None:
                raise RuntimeError(
                    f"Required a1 index {qualified_name} is absent."
                )
            normalized = _a1_normalize_index_definition(definition)
            if any(marker not in normalized for marker in markers):
                raise RuntimeError(
                    f"A1 index definition drift for {qualified_name}: "
                    f"{normalized!r}."
                )
        elif definition is not None:
            raise RuntimeError(
                f"A1-owned index {qualified_name} already exists."
            )


def _a1_preflight(
    bind,
    *,
    expected_view_generation,
    expected_function_generation,
    new_indexes_present,
):
    _a1_require_migration_owner(bind)
    role = bind.execute(
        sa.text(
            """
            SELECT
                rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                rolcanlogin, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = 'app_security_owner'
            """
        )
    ).mappings().one_or_none()
    if role is None:
        raise RuntimeError(
            "Required managed role app_security_owner is absent."
        )
    if any(bool(value) for value in role.values()):
        raise RuntimeError(
            "app_security_owner attributes violate the managed-role contract."
        )
    if not _a1_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    role_count = bind.execute(
        sa.text(
            """
            SELECT count(*)::int
            FROM pg_catalog.pg_roles
            WHERE rolname IN (
                'app_runtime', 'readonly_analytics', 'audit_writer'
            )
            """
        )
    ).scalar_one()
    if role_count != 3:
        raise RuntimeError("Required managed reader/writer roles are absent.")
    schemas = bind.execute(
        sa.text(
            """
            SELECT
                requested.schema_name,
                namespace.oid IS NOT NULL,
                owner.rolname::text
            FROM (
                VALUES ('app_private'::text), ('app_secure'::text)
            ) AS requested(schema_name)
            LEFT JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.nspname = requested.schema_name
            LEFT JOIN pg_catalog.pg_roles AS owner
              ON owner.oid = namespace.nspowner
            ORDER BY requested.schema_name
            """
        )
    ).all()
    observed = {row[0]: (bool(row[1]), row[2]) for row in schemas}
    expected = {
        "app_private": (True, _A1_MIGRATION_OWNER),
        "app_secure": (True, _A1_SECURITY_OWNER),
    }
    if observed != expected:
        raise RuntimeError(
            f"Protected schema owner contract drifted: {observed!r}."
        )
    if any(_a1_public_private_privileges(bind).values()):
        raise RuntimeError("PUBLIC authority on app_private is forbidden.")
    for relation_name in (
        "branch_staff_roles",
        "organization_members",
        "staff_roles",
        "scope_types",
    ):
        allowed = bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                "'app_security_owner', "
                "CAST(:qualified_name AS regclass), 'SELECT')"
            ),
            {"qualified_name": f"public.{relation_name}"},
        ).scalar_one()
        if not allowed:
            raise RuntimeError(
                "app_security_owner lacks SELECT on "
                f"public.{relation_name}."
            )
    _a1_verify_view_contract(bind, expected_view_generation)
    _a1_verify_function_contract(bind, expected_function_generation)
    _a1_verify_inherited_rls_and_policy(bind)
    _a1_verify_index_contract(
        bind, new_indexes_present=new_indexes_present
    )


def _a1_replace_active_view(bind, generation):
    if generation == "forward":
        create_sql = _A1_FORWARD_VIEW_SQL
    elif generation == "predecessor":
        create_sql = _A1_PREDECESSOR_VIEW_SQL
    else:
        raise RuntimeError(f"Unsupported view generation {generation!r}.")
    escaped_comment = _A1_VIEW_COMMENT.replace("'", "''")
    _a1_run_as_security_owner(
        bind,
        (
            "DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT",
            create_sql,
            "REVOKE ALL ON app_secure.v_active_branch_staff_roles "
            "FROM PUBLIC",
            "GRANT SELECT ON app_secure.v_active_branch_staff_roles "
            "TO app_runtime, readonly_analytics",
            "COMMENT ON VIEW app_secure.v_active_branch_staff_roles IS "
            f"'{escaped_comment}'",
        ),
    )
    _a1_verify_view_contract(bind, generation)


def _a1_replace_partition_function(bind, generation):
    if generation == "forward":
        create_sql = _A1_FORWARD_FUNCTION_SQL
    elif generation == "predecessor":
        create_sql = _A1_PREDECESSOR_FUNCTION_SQL
    else:
        raise RuntimeError(
            f"Unsupported function generation {generation!r}."
        )
    owner_window = _a1_prepare_app_private_owner_window(bind)
    _a1_run_as_security_owner(
        bind,
        (
            create_sql,
            "REVOKE ALL ON FUNCTION "
            "app_private.ensure_future_partition(TEXT, INTEGER) "
            "FROM PUBLIC",
        ),
    )
    _a1_restore_app_private_owner_window(bind, owner_window)
    _a1_verify_function_contract(bind, generation)


def _a1_create_new_indexes():
    op.execute(
        "CREATE INDEX ix_roles_active_lookup "
        "ON public.branch_staff_roles("
        "org_id, branch_id, organization_member_id) "
        "WHERE revoked_at IS NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_member_active "
        "ON public.organization_members(org_id, user_id) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_snapshot_active "
        "ON public.member_permission_snapshots(organization_member_id) "
        "WHERE is_stale = FALSE"
    )
    op.execute(
        "CREATE INDEX ix_auth_sessions_active "
        "ON public.auth_sessions(user_id, org_id) "
        "WHERE revoked_at IS NULL"
    )


def _a1_drop_new_indexes():
    op.execute("DROP INDEX public.ix_auth_sessions_active RESTRICT")
    op.execute("DROP INDEX public.ix_snapshot_active RESTRICT")
    op.execute("DROP INDEX public.ix_member_active RESTRICT")
    op.execute("DROP INDEX public.ix_roles_active_lookup RESTRICT")


# A1B2C3D4E5F6_OWNER_CONTEXT_HELPERS_END


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '970059a0665d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = _a1_bind()
    _a1_preflight(
        bind,
        expected_view_generation="predecessor",
        expected_function_generation="predecessor",
        new_indexes_present=False,
    )

    # Phase 15 is already the exact inherited contract. Keep its policy and
    # all three ENABLE/FORCE states unchanged.

    # Phase 16 removes dependencies needed by the later 361 contract step,
    # while preserving all protected-view security and ACL contracts.
    _a1_replace_active_view(bind, "forward")

    # Phase 17 owns four new indexes. ix_audit_org_sequence belongs to the
    # 0026/F71 predecessor and is validation-only here.
    _a1_create_new_indexes()

    # Phase 18 legitimately evolves the predecessor function, but only under
    # its actual owner and an exact, temporary app_private ACL window.
    _a1_replace_partition_function(bind, "forward")

    _a1_verify_inherited_rls_and_policy(bind)
    _a1_verify_index_contract(bind, new_indexes_present=True)
    _a1_verify_view_contract(bind, "forward")
    _a1_verify_function_contract(bind, "forward")
    _a1_require_migration_owner(bind)


def downgrade() -> None:
    bind = _a1_bind()
    _a1_preflight(
        bind,
        expected_view_generation="forward",
        expected_function_generation="forward",
        new_indexes_present=True,
    )

    # Restore the exact 0026/F71 predecessor function definition without
    # changing its owner or ACL.
    _a1_replace_partition_function(bind, "predecessor")

    # Remove only the four indexes introduced by this revision.
    _a1_drop_new_indexes()

    # Restore the exact joined 0029/970 predecessor view under its owner.
    _a1_replace_active_view(bind, "predecessor")

    # Phase 15 was validation-only; its inherited policy and RLS/FORCE states
    # must survive unchanged.
    _a1_verify_inherited_rls_and_policy(bind)
    _a1_verify_index_contract(bind, new_indexes_present=False)
    _a1_verify_view_contract(bind, "predecessor")
    _a1_verify_function_contract(bind, "predecessor")
    _a1_require_migration_owner(bind)
