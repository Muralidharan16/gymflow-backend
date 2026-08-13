"""RBAC Hardening Phase 6 — Permission Snapshots

Phase 6 of the v18.0 hardening plan.

Creates:
  • public.member_permission_snapshots
      — Compiled JSONB permission cache per (member, scope, branch).
      — Keyed by (organization_member_id, org_id, scope_type_id, branch_id).
      — compiled_permissions: sorted JSONB array of permission codes.
      — snapshot_version ties to organization_members.permission_version.
      — is_stale: set TRUE by trigger when roles change (lazy recompute).
      — expires_at: absolute TTL for cache entry.
      — Append-only row semantics: stale rows are never updated in-place,
        a new snapshot row is inserted on recompute (audit trail preserved).

  • app_private.mark_snapshot_stale()
      — AFTER trigger on branch_staff_roles (INSERT/UPDATE/DELETE).
      — Marks all snapshots for the affected member+branch stale.
      — Also bumps organization_members.permission_version.

  • app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT)
      — Computes the full permission set for a member at a given scope.
      — Returns JSONB sorted array of permission codes.
      — Called by application layer to rebuild a stale snapshot.

  • app_secure.v_effective_member_permissions
      — Security-barrier view: most recent non-stale snapshot per member+branch.

RLS:
  • tenant_isolation_permission_snapshots
      — Fail-closed; filters by org_id GUC.

Design notes:
  • Snapshots are NOT the source of truth — branch_staff_roles is.
  • Snapshots are a pre-compiled read cache for RLS and API responses.
  • TTL (expires_at) is 1 hour by default — refreshed on any role mutation.
  • Application must call compile_member_permissions() to rebuild on cache miss.

Revision ID: 0027_rbac_p6_perm_snapshots
Revises: 0026_rbac_p5_audit_log
Create Date: 2026-05-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_rbac_p6_perm_snapshots"
down_revision = "0026_rbac_p5_audit_log"
branch_labels = None
depends_on = None


# RB1M2S_0027_COMPLETE_OWNER_CONTEXT_AUTHORITY_HELPERS_START

_RB1M2S_MIGRATION_OWNER = "migration_owner"
_RB1M2S_SECURITY_OWNER = "app_security_owner"
_RB1M2S_PRIVATE_SCHEMA = "app_private"
_RB1M2S_SECURE_SCHEMA = "app_secure"
_RB1M2S_SNAPSHOT_TABLE = "public.member_permission_snapshots"
_RB1M2S_TOUCH_FUNCTION = "app_private.touch_updated_at()"
_RB1M2S_MARK_FUNCTION = "app_private.mark_snapshot_stale()"
_RB1M2S_COMPILE_FUNCTION = (
    "app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT)"
)
_RB1M2S_VIEW = "app_secure.v_effective_member_permissions"


def _rb1m2s_identity(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()


def _rb1m2s_require_migration_owner(bind):
    identity = _rb1m2s_identity(bind)
    if (
        identity["session_user_name"] != _RB1M2S_MIGRATION_OWNER
        or identity["current_user_name"] != _RB1M2S_MIGRATION_OWNER
    ):
        raise RuntimeError(
            "Revision-0027 requires session_user=current_user="
            f"{_RB1M2S_MIGRATION_OWNER}; observed "
            f"{dict(identity)!r}."
        )


def _rb1m2s_can_set_security_owner(bind):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_has_role(
                    current_user,
                    :role_name,
                    'SET'
                )
                """
            ),
            {"role_name": _RB1M2S_SECURITY_OWNER},
        ).scalar_one()
    )


def _rb1m2s_run_as_security_owner(bind, statements):
    _rb1m2s_require_migration_owner(bind)
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    for statement in statements:
        bind.execute(sa.text(statement))
    bind.execute(sa.text("RESET ROLE"))
    _rb1m2s_require_migration_owner(bind)


def _rb1m2s_direct_private_create_acl(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                grantee_role.rolname::text AS grantee_name,
                acl.privilege_type::text AS privilege_type,
                acl.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                namespace.nspacl
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl.grantor
            JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl.grantee
            WHERE namespace.nspname = :schema_name
              AND grantee_role.rolname = :target_role
              AND acl.privilege_type = 'CREATE'
            ORDER BY
                grantor_role.rolname,
                grantee_role.rolname,
                acl.privilege_type,
                acl.is_grantable
            """
        ),
        {
            "schema_name": _RB1M2S_PRIVATE_SCHEMA,
            "target_role": _RB1M2S_SECURITY_OWNER,
        },
    ).mappings().all()
    return tuple(
        (
            row["grantor_name"],
            row["grantee_name"],
            row["privilege_type"],
            bool(row["is_grantable"]),
        )
        for row in rows
    )


def _rb1m2s_preflight(bind):
    _rb1m2s_require_migration_owner(bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                requested.schema_name,
                namespace.oid IS NOT NULL AS schema_exists,
                owner_role.rolname::text AS owner_name
            FROM (
                VALUES
                    ('app_private'::text),
                    ('app_secure'::text)
            ) AS requested(schema_name)
            LEFT JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.nspname = requested.schema_name
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = namespace.nspowner
            ORDER BY requested.schema_name
            """
        )
    ).mappings().all()
    schemas = {row["schema_name"]: row for row in rows}
    if not schemas[_RB1M2S_PRIVATE_SCHEMA]["schema_exists"]:
        raise RuntimeError("Revision-0027 requires app_private.")
    if (
        schemas[_RB1M2S_PRIVATE_SCHEMA]["owner_name"]
        != _RB1M2S_MIGRATION_OWNER
    ):
        raise RuntimeError(
            "Revision-0027 requires app_private owner migration_owner."
        )
    if not schemas[_RB1M2S_SECURE_SCHEMA]["schema_exists"]:
        raise RuntimeError("Revision-0027 requires app_secure.")
    if (
        schemas[_RB1M2S_SECURE_SCHEMA]["owner_name"]
        != _RB1M2S_SECURITY_OWNER
    ):
        raise RuntimeError(
            "Revision-0027 requires app_secure owner "
            "app_security_owner."
        )
    role = bind.execute(
        sa.text(
            """
            SELECT
                rolcanlogin,
                rolinherit,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _RB1M2S_SECURITY_OWNER},
    ).mappings().one_or_none()
    if role is None:
        raise RuntimeError("Revision-0027 target owner role is absent.")
    if role["rolcanlogin"] or role["rolinherit"] or role["rolbypassrls"]:
        raise RuntimeError(
            "Revision-0027 app_security_owner attributes are unsafe."
        )
    if not _rb1m2s_can_set_security_owner(bind):
        raise RuntimeError(
            "Revision-0027 migration identity cannot SET ROLE "
            "app_security_owner."
        )
    for schema_name in (_RB1M2S_PRIVATE_SCHEMA, "public"):
        if not bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_schema_privilege(
                    :role_name,
                    :schema_name,
                    'USAGE'
                )
                """
            ),
            {
                "role_name": _RB1M2S_SECURITY_OWNER,
                "schema_name": schema_name,
            },
        ).scalar_one():
            raise RuntimeError(
                "Revision-0027 app_security_owner lacks USAGE on "
                f"{schema_name}."
            )


def _rb1m2s_prepare_owner_transfer(bind):
    before = _rb1m2s_direct_private_create_acl(bind)
    has_create = bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_schema_privilege(
                    :role_name,
                    :schema_name,
                    'CREATE'
                )
                """
            ),
            {
                "role_name": _RB1M2S_SECURITY_OWNER,
                "schema_name": _RB1M2S_PRIVATE_SCHEMA,
            },
        ).scalar_one()
    )
    added = not has_create
    if added:
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_private "
                "TO app_security_owner"
            )
        )
    return {"before": before, "added": added}


def _rb1m2s_restore_owner_transfer(bind, state):
    if state["added"]:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_private "
                "FROM app_security_owner"
            )
        )
    after = _rb1m2s_direct_private_create_acl(bind)
    if after != state["before"]:
        raise RuntimeError(
            "Revision-0027 app_private CREATE ACL restoration drift: "
            f"before={state['before']!r}, after={after!r}."
        )


def _rb1m2s_verify_function(
    bind,
    signature,
    *,
    expected_execute_roles,
):
    row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(function_data.proowner)::text
                    AS owner_name,
                function_data.prosecdef AS security_definer,
                function_data.proconfig
            FROM pg_catalog.pg_proc AS function_data
            WHERE function_data.oid = pg_catalog.to_regprocedure(
                :signature
            )
            """
        ),
        {"signature": signature},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            f"Revision-0027 function is absent: {signature}."
        )
    if row["owner_name"] != _RB1M2S_SECURITY_OWNER:
        raise RuntimeError(
            f"Revision-0027 function owner drift for {signature}: "
            f"{row['owner_name']!r}."
        )
    if not row["security_definer"]:
        raise RuntimeError(
            f"Revision-0027 function is not SECURITY DEFINER: "
            f"{signature}."
        )
    acl_rows = bind.execute(
        sa.text(
            """
            SELECT
                CASE
                    WHEN acl.grantee = 0 THEN 'PUBLIC'
                    ELSE grantee_role.rolname::text
                END AS grantee_name,
                acl.privilege_type::text AS privilege_type,
                acl.is_grantable AS is_grantable
            FROM pg_catalog.pg_proc AS function_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    function_data.proacl,
                    pg_catalog.acldefault(
                        'f'::"char",
                        function_data.proowner
                    )
                )
            ) AS acl
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl.grantee
            WHERE function_data.oid = pg_catalog.to_regprocedure(
                :signature
            )
              AND acl.privilege_type = 'EXECUTE'
            ORDER BY grantee_name, acl.is_grantable
            """
        ),
        {"signature": signature},
    ).mappings().all()
    observed = {
        row["grantee_name"]
        for row in acl_rows
        if not row["is_grantable"]
    }
    expected = {_RB1M2S_SECURITY_OWNER, *expected_execute_roles}
    if observed != expected:
        raise RuntimeError(
            f"Revision-0027 function EXECUTE ACL drift for "
            f"{signature}: observed={sorted(observed)!r}, "
            f"expected={sorted(expected)!r}."
        )


# RB1M2T_0027_PUBLIC_PSEUDOROLE_ACL_HELPERS_START


def _rb1m2t_public_has_function_execute(bind, signature):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_proc AS function_data
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(
                            function_data.proacl,
                            pg_catalog.acldefault(
                                'f'::"char",
                                function_data.proowner
                            )
                        )
                    ) AS acl
                    WHERE function_data.oid =
                        pg_catalog.to_regprocedure(:signature)
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        ).scalar_one()
    )


def _rb1m2u_public_has_relation_select(
    bind,
    schema_name,
    relation_name,
):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(
                            relation.relacl,
                            pg_catalog.acldefault(
                                CASE
                                    WHEN relation.relkind = 'S'
                                        THEN 's'::"char"
                                    ELSE 'r'::"char"
                                END,
                                relation.relowner
                            )
                        )
                    ) AS acl
                    WHERE namespace.nspname = :schema_name
                      AND relation.relname = :relation_name
                      AND relation.relkind IN (
                          'r',
                          'p',
                          'v',
                          'm',
                          'f',
                          'S'
                      )
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'SELECT'
                )
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
            },
        ).scalar_one()
    )


# RB1M2T_0027_PUBLIC_PSEUDOROLE_ACL_HELPERS_END

# RB1M2V_0027_PROTECTED_VIEW_READER_ACL_HELPER_START


def _rb1m2v_role_has_direct_relation_select(
    bind,
    schema_name,
    relation_name,
    grantee_name,
    grantor_name,
):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    JOIN pg_catalog.pg_roles AS grantee_role
                      ON grantee_role.rolname = :grantee_name
                    JOIN pg_catalog.pg_roles AS grantor_role
                      ON grantor_role.rolname = :grantor_name
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(
                            relation.relacl,
                            pg_catalog.acldefault(
                                CASE
                                    WHEN relation.relkind = 'S'
                                        THEN 's'::"char"
                                    ELSE 'r'::"char"
                                END,
                                relation.relowner
                            )
                        )
                    ) AS acl
                    WHERE namespace.nspname = :schema_name
                      AND relation.relname = :relation_name
                      AND relation.relkind IN (
                          'r',
                          'p',
                          'v',
                          'm',
                          'f',
                          'S'
                      )
                      AND acl.grantee = grantee_role.oid
                      AND acl.grantor = grantor_role.oid
                      AND acl.privilege_type = 'SELECT'
                      AND acl.is_grantable IS FALSE
                )
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
                "grantee_name": grantee_name,
                "grantor_name": grantor_name,
            },
        ).scalar_one()
    )


# RB1M2V_0027_PROTECTED_VIEW_READER_ACL_HELPER_END

def _rb1m2s_create_touch_trigger(bind):
    function_row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(function_data.proowner)::text
                    AS owner_name,
                function_data.prorettype = 'trigger'::regtype
                    AS returns_trigger,
                function_data.prosecdef AS security_definer,
                (
                    'search_path=pg_catalog' = ANY(
                        COALESCE(
                            function_data.proconfig,
                            ARRAY[]::text[]
                        )
                    )
                ) AS safe_search_path
            FROM pg_catalog.pg_proc AS function_data
            WHERE function_data.oid = pg_catalog.to_regprocedure(
                :signature
            )
            """
        ),
        {"signature": _RB1M2S_TOUCH_FUNCTION},
    ).mappings().one_or_none()
    if function_row is None:
        raise RuntimeError(
            "Revision-0027 touch_updated_at() is absent."
        )
    if (
        function_row["owner_name"] != _RB1M2S_SECURITY_OWNER
        or not function_row["returns_trigger"]
        or not function_row["security_definer"]
        or not function_row["safe_search_path"]
    ):
        raise RuntimeError(
            "Revision-0027 touch_updated_at() contract is unsafe: "
            f"{dict(function_row)!r}."
        )
    public_execute = _rb1m2t_public_has_function_execute(
        bind,
        _RB1M2S_TOUCH_FUNCTION,
    )
    if public_execute:
        raise RuntimeError(
            "Revision-0027 PUBLIC can execute touch_updated_at()."
        )
    table_row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(relation.relowner)::text
                    AS owner_name,
                relation.relkind::text AS relation_kind
            FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = pg_catalog.to_regclass(
                :relation_name
            )
            """
        ),
        {"relation_name": _RB1M2S_SNAPSHOT_TABLE},
    ).mappings().one_or_none()
    if table_row is None:
        raise RuntimeError(
            "Revision-0027 snapshot table is absent."
        )
    if (
        table_row["owner_name"] != _RB1M2S_MIGRATION_OWNER
        or table_row["relation_kind"] not in {"r", "p"}
    ):
        raise RuntimeError(
            "Revision-0027 snapshot table owner/kind drift: "
            f"{dict(table_row)!r}."
        )
    already_has_trigger = bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_table_privilege(
                    :role_name,
                    :relation_name,
                    'TRIGGER'
                )
                """
            ),
            {
                "role_name": _RB1M2S_SECURITY_OWNER,
                "relation_name": _RB1M2S_SNAPSHOT_TABLE,
            },
        ).scalar_one()
    )
    if already_has_trigger:
        raise RuntimeError(
            "Revision-0027 app_security_owner already has "
            "snapshot-table TRIGGER authority."
        )
    bind.execute(
        sa.text(
            "GRANT TRIGGER ON TABLE "
            "public.member_permission_snapshots "
            "TO app_security_owner"
        )
    )
    _rb1m2s_run_as_security_owner(
        bind,
        (
            """
            CREATE TRIGGER trg_touch_perm_snapshot_updated_at
                BEFORE UPDATE
                ON public.member_permission_snapshots
                FOR EACH ROW
                EXECUTE FUNCTION app_private.touch_updated_at()
            """,
        ),
    )
    bind.execute(
        sa.text(
            "REVOKE TRIGGER ON TABLE "
            "public.member_permission_snapshots "
            "FROM app_security_owner"
        )
    )
    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                :role_name,
                :relation_name,
                'TRIGGER'
            )
            """
        ),
        {
            "role_name": _RB1M2S_SECURITY_OWNER,
            "relation_name": _RB1M2S_SNAPSHOT_TABLE,
        },
    ).scalar_one():
        raise RuntimeError(
            "Revision-0027 temporary table TRIGGER authority leaked."
        )


def _rb1m2s_create_secure_view(bind):
    target = bind.execute(
        sa.text(
            """
            SELECT
                relation.relkind::text AS relation_kind,
                pg_catalog.pg_get_userbyid(relation.relowner)::text
                    AS owner_name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'app_secure'
              AND relation.relname =
                  'v_effective_member_permissions'
            """
        )
    ).mappings().one_or_none()
    if target is not None:
        raise RuntimeError(
            "Revision-0027 target view already exists: "
            f"{dict(target)!r}."
        )
    for relation_name in (
        "public.member_permission_snapshots",
        "public.scope_types",
    ):
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.to_regclass(:relation_name)
                       IS NOT NULL
                """
            ),
            {"relation_name": relation_name},
        ).scalar_one() is not True:
            raise RuntimeError(
                "Revision-0027 view base relation is absent: "
                f"{relation_name}."
            )
    if not bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                :role_name,
                'public.scope_types',
                'SELECT'
            )
            """
        ),
        {"role_name": _RB1M2S_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "Revision-0027 predecessor scope_types SELECT "
            "authority is missing."
        )
    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                :role_name,
                'public.member_permission_snapshots',
                'SELECT'
            )
            """
        ),
        {"role_name": _RB1M2S_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "Revision-0027 app_security_owner already has "
            "snapshot-table SELECT authority."
        )
    bind.execute(
        sa.text(
            "GRANT SELECT ON TABLE "
            "public.member_permission_snapshots "
            "TO app_security_owner"
        )
    )
    _rb1m2s_run_as_security_owner(
        bind,
        (
            """
            CREATE OR REPLACE VIEW
                app_secure.v_effective_member_permissions
            WITH (
                security_barrier = true,
                security_invoker = true
            )
            AS
            SELECT
                mps.id,
                mps.org_id,
                mps.organization_member_id,
                mps.scope_type_id,
                st.code AS scope_code,
                mps.branch_id,
                mps.compiled_permissions,
                mps.snapshot_version,
                mps.is_stale,
                mps.expires_at,
                mps.created_at,
                mps.updated_at
            FROM public.member_permission_snapshots AS mps
            JOIN public.scope_types AS st
              ON st.id = mps.scope_type_id
            WHERE mps.is_stale = FALSE
              AND mps.expires_at > clock_timestamp()
            """,
            """
            REVOKE ALL ON TABLE
                app_secure.v_effective_member_permissions
            FROM PUBLIC
            """,
            """
            GRANT SELECT ON TABLE
                app_secure.v_effective_member_permissions
            TO app_runtime, readonly_analytics
            """,
            """
            COMMENT ON VIEW
                app_secure.v_effective_member_permissions
            IS
                'Security-barrier, security-invoker view: '
                'non-stale, non-expired permission snapshots.'
            """,
        ),
    )
    bind.execute(
        sa.text(
            "REVOKE SELECT ON TABLE "
            "public.member_permission_snapshots "
            "FROM app_security_owner"
        )
    )
    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                :role_name,
                'public.member_permission_snapshots',
                'SELECT'
            )
            """
        ),
        {"role_name": _RB1M2S_SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError(
            "Revision-0027 temporary snapshot SELECT leaked."
        )
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation.relkind::text AS relation_kind,
                pg_catalog.pg_get_userbyid(relation.relowner)::text
                    AS owner_name,
                relation.reloptions
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'app_secure'
              AND relation.relname =
                  'v_effective_member_permissions'
            """
        )
    ).mappings().one()
    options = set(row["reloptions"] or ())
    if (
        row["relation_kind"] != "v"
        or row["owner_name"] != _RB1M2S_SECURITY_OWNER
        or not {
            "security_barrier=true",
            "security_invoker=true",
        }.issubset(options)
    ):
        raise RuntimeError(
            "Revision-0027 view owner/options drift: "
            f"{dict(row)!r}."
        )
    if _rb1m2u_public_has_relation_select(
        bind,
        _RB1M2S_SECURE_SCHEMA,
        "v_effective_member_permissions",
    ):
        raise RuntimeError(
            "Revision-0027 PUBLIC can SELECT the protected view."
        )
    for role_name in ("app_runtime", "readonly_analytics"):
        if not _rb1m2v_role_has_direct_relation_select(
            bind,
            _RB1M2S_SECURE_SCHEMA,
            "v_effective_member_permissions",
            role_name,
            _RB1M2S_SECURITY_OWNER,
        ):
            raise RuntimeError(
                "Revision-0027 protected-view direct reader grant is "
                f"missing for {role_name}."
            )


def _rb1m2s_drop_secure_view(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation.relkind::text AS relation_kind,
                pg_catalog.pg_get_userbyid(relation.relowner)::text
                    AS owner_name,
                relation.reloptions
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'app_secure'
              AND relation.relname =
                  'v_effective_member_permissions'
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            "Revision-0027 protected view is absent during downgrade."
        )
    options = set(row["reloptions"] or ())
    if (
        row["relation_kind"] != "v"
        or row["owner_name"] != _RB1M2S_SECURITY_OWNER
        or not {
            "security_barrier=true",
            "security_invoker=true",
        }.issubset(options)
    ):
        raise RuntimeError(
            "Revision-0027 downgrade view contract drift: "
            f"{dict(row)!r}."
        )
    _rb1m2s_run_as_security_owner(
        bind,
        (
            """
            DROP VIEW
                app_secure.v_effective_member_permissions
            RESTRICT
            """,
        ),
    )


def _rb1m2s_drop_owned_functions(bind):
    _rb1m2s_run_as_security_owner(
        bind,
        (
            """
            DROP FUNCTION
                app_private.compile_member_permissions(
                    UUID,
                    UUID,
                    UUID,
                    SMALLINT
                )
            RESTRICT
            """,
            """
            DROP FUNCTION
                app_private.mark_snapshot_stale()
            RESTRICT
            """,
        ),
    )

# RB1M2S_0027_COMPLETE_OWNER_CONTEXT_AUTHORITY_HELPERS_END


def upgrade() -> None:
    bind = op.get_bind()
    _rb1m2s_preflight(bind)

    # ── 1. member_permission_snapshots table ──────────────────────────────
    op.execute("""
        CREATE TABLE public.member_permission_snapshots (
            id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                 UUID        NOT NULL
                                   REFERENCES public.organizations(id) ON DELETE RESTRICT,
            organization_member_id UUID        NOT NULL,
            scope_type_id          SMALLINT    NOT NULL DEFAULT 2
                                   REFERENCES public.scope_types(id) ON DELETE RESTRICT,
            branch_id              UUID        NULL,

            -- Permission payload
            compiled_permissions   JSONB       NOT NULL DEFAULT '[]',
            snapshot_version       BIGINT      NOT NULL DEFAULT 1,

            -- Cache control
            is_stale               BOOLEAN     NOT NULL DEFAULT FALSE,
            expires_at             TIMESTAMPTZ NOT NULL
                                   DEFAULT clock_timestamp() + interval '1 hour',

            -- Composite FK: guarantees member belongs to the same org
            CONSTRAINT fk_perm_snap_member_org
                FOREIGN KEY (organization_member_id, org_id)
                REFERENCES public.organization_members(id, org_id)
                ON DELETE CASCADE,

            -- Uniqueness: one current snapshot per member+scope+branch
            CONSTRAINT uq_perm_snap_member_scope_branch
                UNIQUE (organization_member_id, org_id, scope_type_id, branch_id),

            -- Self-consistency: compiled_permissions must be a JSON array
            CONSTRAINT chk_perm_snap_is_array
                CHECK (jsonb_typeof(compiled_permissions) = 'array'),

            -- Freshness: expires_at must always be in the future at insert time
            -- (enforced by trigger, not CHECK, to avoid volatile function issues)

            created_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.member_permission_snapshots IS
            'Pre-compiled JSONB permission cache per (member, org, scope, branch). '
            'NOT the source of truth — branch_staff_roles is. '
            'is_stale=TRUE means the snapshot needs recomputation by the application. '
            'Append-only semantics: stale rows trigger a fresh INSERT on recompute. '
            'TTL: 1 hour by default; refreshed on any role mutation via trigger.';
    """)

    op.execute("""
        COMMENT ON CONSTRAINT uq_perm_snap_member_scope_branch
        ON public.member_permission_snapshots IS
            'One active snapshot row per (member, org, scope, branch). '
            'On recompute, application UPSERTs on this constraint.';
    """)

    # ── 2. touch_updated_at trigger (reuse from Phase 3) ─────────────────
    _rb1m2s_create_touch_trigger(bind)

    # ── 3. Indexes ────────────────────────────────────────────────────────

    # Primary access path: find current fresh snapshot for a member at a branch.
    # Note: expires_at filter is applied at query time (volatile fn not allowed in index predicates).
    op.execute("""
        CREATE INDEX ix_perm_snap_member_branch_fresh
        ON public.member_permission_snapshots(organization_member_id, branch_id, expires_at)
        WHERE is_stale = FALSE;
    """)

    # Org-level sweep: find all stale snapshots for a tenant (refresh job)
    op.execute("""
        CREATE INDEX ix_perm_snap_org_stale
        ON public.member_permission_snapshots(org_id)
        WHERE is_stale = TRUE;
    """)

    # Version-based invalidation lookup
    op.execute("""
        CREATE INDEX ix_perm_snap_member_version
        ON public.member_permission_snapshots(organization_member_id, snapshot_version);
    """)

    # ── 4. RLS ────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE public.member_permission_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.member_permission_snapshots FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_permission_snapshots
        ON public.member_permission_snapshots
        FOR ALL
        USING (
            org_id = current_setting('app.current_org_id', false)::uuid
        )
        WITH CHECK (
            org_id = current_setting('app.current_org_id', false)::uuid
        );
    """)

    # ── 5. Grants ─────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.member_permission_snapshots
        TO app_runtime;
    """)
    op.execute("GRANT SELECT ON public.member_permission_snapshots TO readonly_analytics;")

    # ── 6. mark_snapshot_stale() trigger function ─────────────────────────
    # Fired AFTER INSERT/UPDATE/DELETE on branch_staff_roles.
    # Marks affected member+branch+org snapshots as stale.
    # Also increments permission_version on organization_members to signal
    # any cached GUC-based permission checks are expired.
    owner_state = _rb1m2s_prepare_owner_transfer(bind)

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.mark_snapshot_stale()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = off
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_member_id UUID;
            v_org_id    UUID;
            v_branch_id UUID;
        BEGIN
            -- Resolve the affected member from either NEW or OLD row
            IF TG_OP = 'DELETE' THEN
                v_member_id := OLD.organization_member_id;
                v_org_id    := OLD.org_id;
                v_branch_id := OLD.branch_id;
            ELSE
                v_member_id := NEW.organization_member_id;
                v_org_id    := NEW.org_id;
                v_branch_id := NEW.branch_id;
            END IF;

            -- Only process new-model rows (organization_member_id is set)
            IF v_member_id IS NULL THEN
                RETURN COALESCE(NEW, OLD);
            END IF;

            -- Mark all snapshots for this member+branch stale
            UPDATE public.member_permission_snapshots
            SET    is_stale   = TRUE,
                   updated_at = clock_timestamp()
            WHERE  organization_member_id = v_member_id
              AND  org_id                 = v_org_id
              AND  (branch_id = v_branch_id OR branch_id IS NULL);

            -- Bump permission_version on the member record so any
            -- session-level GUC permission cache knows to re-fetch
            UPDATE public.organization_members
            SET    permission_version = permission_version + 1,
                   updated_at         = clock_timestamp()
            WHERE  id     = v_member_id
              AND  org_id = v_org_id;

            RETURN COALESCE(NEW, OLD);
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.mark_snapshot_stale() FROM PUBLIC;")

    op.execute("""
        COMMENT ON FUNCTION app_private.mark_snapshot_stale() IS
            'AFTER trigger on branch_staff_roles. '
            'Marks all permission snapshots stale for the affected member+branch. '
            'Bumps organization_members.permission_version for session-level invalidation.';
    """)

    # Attach to branch_staff_roles — fires for new-model rows only
    op.execute("""
        CREATE TRIGGER trg_invalidate_perm_snapshot
            AFTER INSERT OR UPDATE OR DELETE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.mark_snapshot_stale();
    """)

    op.execute("ALTER FUNCTION app_private.mark_snapshot_stale() OWNER TO app_security_owner;")
    _rb1m2s_verify_function(
        bind,
        _RB1M2S_MARK_FUNCTION,
        expected_execute_roles=(),
    )

    # ── 7. compile_member_permissions() callable ──────────────────────────
    # Called by the application to rebuild a stale snapshot.
    # Returns sorted JSONB array of permission codes.
    # The application then UPSERTs into member_permission_snapshots.
    # row_security = off: function queries branch_staff_roles directly,
    # bypassing RLS (it runs as a trusted internal caller).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.compile_member_permissions(
            p_organization_member_id UUID,
            p_org_id                 UUID,
            p_branch_id              UUID,
            p_scope_type_id          SMALLINT DEFAULT 2
        )
        RETURNS JSONB
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = off
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_permission_codes JSONB;
        BEGIN
            -- Derive the permission codes from active role assignments.
            -- Joins branch_staff_roles → staff_roles → role_permission_map.
            -- For now, uses a hardcoded mapping table (role_id → permission codes).
            -- This will be replaced by public.role_permission_events in Phase 7.
            --
            -- Permission derivation logic:
            --   1. Find all active (non-revoked, non-deleted) role assignments
            --      for this member at the given branch.
            --   2. For each role, look up its permission codes in staff_roles.
            --   3. Aggregate, deduplicate, and sort.

            SELECT jsonb_agg(DISTINCT p.code ORDER BY p.code)
            INTO   v_permission_codes
            FROM   public.branch_staff_roles bsr
            JOIN   public.staff_roles sr  ON sr.id = bsr.role_id
            JOIN   public.permissions p   ON TRUE
            WHERE  bsr.organization_member_id = p_organization_member_id
              AND  bsr.org_id                 = p_org_id
              AND  bsr.branch_id              = p_branch_id
              AND  bsr.scope_type_id          = p_scope_type_id
              AND  bsr.revoked_at             IS NULL
              AND  bsr.deleted_at             IS NULL
              AND  bsr.effective_from         <= clock_timestamp()
              AND (bsr.effective_to           IS NULL OR bsr.effective_to > clock_timestamp())
              -- Permission codes are derived from role hierarchy level
              -- owner(100): all permissions
              -- admin(80): all except org.settings.update
              -- manager(60): branch ops + staff_roles read/assign/revoke + members
              -- trainer(40): branch.read, members.read
              -- receptionist(20): branch.read, members.read, members.invite
              -- auditor(10): audit.read, branch.read
              AND  CASE
                  WHEN sr.hierarchy_level >= 100 THEN TRUE
                  WHEN sr.hierarchy_level >= 80  THEN p.code NOT IN ('org.settings.update')
                  WHEN sr.hierarchy_level >= 60  THEN p.code IN (
                      'branch.read','branch.update','branch.suspend',
                      'staff_roles.read','staff_roles.assign','staff_roles.revoke',
                      'members.read','members.invite','members.suspend'
                  )
                  WHEN sr.hierarchy_level >= 40  THEN p.code IN ('branch.read','members.read')
                  WHEN sr.hierarchy_level >= 20  THEN p.code IN ('branch.read','members.read','members.invite')
                  WHEN sr.hierarchy_level >= 10  THEN p.code IN ('audit.read','branch.read')
                  ELSE FALSE
              END;

            -- Return empty array if no permissions found (not NULL)
            RETURN COALESCE(v_permission_codes, '[]'::jsonb);
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) TO app_runtime;")

    op.execute("""
        COMMENT ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) IS
            'Recomputes the permission code set for a member at a branch/scope. '
            'Returns sorted JSONB array. Application UPSERTs result into '
            'member_permission_snapshots on cache miss or stale detection. '
            'Role→permission mapping is inline here; will be table-driven in Phase 7.';
    """)

    op.execute("ALTER FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) OWNER TO app_security_owner;")
    _rb1m2s_verify_function(
        bind,
        _RB1M2S_COMPILE_FUNCTION,
        expected_execute_roles=("app_runtime",),
    )
    _rb1m2s_restore_owner_transfer(bind, owner_state)

    # ── 8. Security barrier + security-invoker view ───────────────────────
    _rb1m2s_create_secure_view(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _rb1m2s_preflight(bind)

    # Protected view must be removed by its owner and without CASCADE.
    _rb1m2s_drop_secure_view(bind)

    # Triggers are table-owned and must be removed before the functions.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_invalidate_perm_snapshot "
        "ON public.branch_staff_roles"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_touch_perm_snapshot_updated_at "
        "ON public.member_permission_snapshots"
    )

    # Both SECURITY DEFINER functions are owned by app_security_owner.
    _rb1m2s_drop_owned_functions(bind)

    # RLS
    op.execute("DROP POLICY IF EXISTS tenant_isolation_permission_snapshots ON public.member_permission_snapshots;")

    # Indexes
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_member_version;")
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_org_stale;")
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_member_branch_fresh;")

    # Table
    op.execute("DROP TABLE IF EXISTS public.member_permission_snapshots RESTRICT;")
