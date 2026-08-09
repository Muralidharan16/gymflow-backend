"""RBAC Hardening Phase 4 — branch_staff_roles Expand Step

Phase 4 (Expand) of the v18.0 hardening plan.

This is the EXPAND step of the Expand/Contract migration pattern.
Old columns (user_id, role ENUM) are kept intact — existing rows and
application code continue to work. New columns are added alongside.

Adds to existing public.branch_staff_roles:
  • organization_member_id UUID NULL  — new tenancy-scoped actor reference
  • role_id SMALLINT NULL             — replaces role ENUM (refs staff_roles)
  • scope_type_id SMALLINT NOT NULL DEFAULT 2  — refs scope_types (default=branch)
  • assignment_source VARCHAR(32) NOT NULL DEFAULT 'dashboard'

New constraints (NOT VALID — added without full table scan lock):
  • fk_bsr_member_id     → organization_members(id)
  • fk_bsr_member_org    → organization_members(id, org_id) composite integrity
  • fk_bsr_role_id       → staff_roles(id)
  • fk_bsr_scope_type_id → scope_types(id)
  • chk_bsr_assignment_src
  • chk_bsr_revocation_from
  • chk_bsr_revocation_to

New exclusion constraint (scoped to rows with organization_member_id set):
  • ex_branch_role_overlap_v2  DEFERRABLE INITIALLY IMMEDIATE

New triggers:
  • app_private.validate_effective_from_window()  — scheduling drift guard
  • app_private.validate_rls_context_match()      — org_id payload poisoning guard

New indexes:
  • ix_bsr_member_active  — primary active lookup for new model
  • ix_bsr_owner_per_org  — supports single-owner enforcement

Hardened RLS:
  • Replaces old tenant_isolation_staff_roles policy
  • fail-closed GUC (false = raises error if unset)
  • soft-delete filter in both USING and WITH CHECK
  • app.can_read_staff_roles GUC pre-authorization

Security barrier view:
  • app_secure.v_active_branch_staff_roles

NOTE: Old columns (user_id, role) remain. Contract step is Phase 8.
NOTE: FK constraints added NOT VALID — run VALIDATE separately post-deploy.

Revision ID: 0025_rbac_p4_bsr_expand
Revises: 0024_rbac_p3_org_members
Create Date: 2026-05-23
"""

from alembic import op
import json
import sqlalchemy as sa

revision = "0025_rbac_p4_bsr_expand"
down_revision = "0024_rbac_p3_org_members"
branch_labels = None
depends_on = None


# RB1M2A_0025_COMPLETE_OWNER_CONTEXT_HELPERS_START
# Frozen revision-local contract. Do not import owner-context logic from
# another migration or mutable application module.
_RB1M2A_PRIVATE_SCHEMA = "app_private"
_RB1M2A_SECURE_SCHEMA = "app_secure"
_RB1M2A_TARGET_OWNER = "app_security_owner"
_RB1M2A_VIEW = "app_secure.v_active_branch_staff_roles"
_RB1M2A_VIEW_STATE_TABLE = "app_private.migration_0025_view_acl_state"
_RB1M2A_FUNCTIONS = (
    "app_private.validate_effective_from_window()",
    "app_private.validate_rls_context_match()",
)
_RB1M2A_TRIGGER_MAP = (
    (
        "trg_bsr_validate_effective_from",
        "app_private.validate_effective_from_window()",
    ),
    (
        "trg_bsr_validate_rls_context",
        "app_private.validate_rls_context_match()",
    ),
)
_RB1M2A_BASE_RELATIONS = (
    ("public", "branch_staff_roles"),
    ("public", "staff_roles"),
    ("public", "scope_types"),
)
_RB1M2A_ALLOWED_SET_ROLES = {
    "migration_owner",
    "app_security_owner",
    "app_rls_executor",
}


def _rb1m2a_identity(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()
    return {
        "session_user_name": row["session_user_name"],
        "current_user_name": row["current_user_name"],
    }


def _rb1m2a_require_migration_owner(bind):
    identity = _rb1m2a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1M2A requires session_user=migration_owner; "
            f"observed {identity['session_user_name']!r}."
        )
    if identity["current_user_name"] != "migration_owner":
        raise RuntimeError(
            "RB1M2A requires current_user=migration_owner; "
            f"observed {identity['current_user_name']!r}."
        )


def _rb1m2a_can_set_role(bind, role_name):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_has_role(
                session_user,
                CAST(:role_name AS name),
                'SET'
            )
            """
        ),
        {"role_name": role_name},
    ).scalar_one() is True


def _rb1m2a_has_schema_privilege(bind, role_name, schema_name, privilege):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role_name AS name),
                CAST(:schema_name AS name),
                :privilege
            )
            """
        ),
        {
            "role_name": role_name,
            "schema_name": schema_name,
            "privilege": privilege,
        },
    ).scalar_one() is True


def _rb1m2a_has_table_privilege(bind, role_name, relation_oid, privilege):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                CAST(:role_name AS name),
                CAST(:relation_oid AS oid),
                :privilege
            )
            """
        ),
        {
            "role_name": role_name,
            "relation_oid": relation_oid,
            "privilege": privilege,
        },
    ).scalar_one() is True

def _rb1m2a_run_as_role(bind, role_name, sql):
    _rb1m2a_require_migration_owner(bind)
    if role_name not in _RB1M2A_ALLOWED_SET_ROLES:
        raise RuntimeError(f"RB1M2A refuses unapproved owner role {role_name!r}.")
    statements = sql if isinstance(sql, (tuple, list)) else (sql,)
    if role_name == "migration_owner":
        for statement in statements:
            bind.execute(sa.text(statement))
        _rb1m2a_require_migration_owner(bind)
        return
    if not _rb1m2a_can_set_role(bind, role_name):
        raise RuntimeError(
            f"migration_owner cannot SET ROLE required owner {role_name!r}."
        )
    if role_name == "app_security_owner":
        bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    elif role_name == "app_rls_executor":
        bind.execute(sa.text("SET LOCAL ROLE app_rls_executor"))
    else:  # pragma: no cover - guarded above
        raise RuntimeError(f"Unhandled bounded role {role_name!r}.")
    identity = _rb1m2a_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError("SET LOCAL ROLE changed session_user.")
    if identity["current_user_name"] != role_name:
        raise RuntimeError(
            f"SET LOCAL ROLE did not enter {role_name!r}: {identity!r}."
        )

    # SET LOCAL ROLE is transaction-scoped. If protected DDL raises, the
    # surrounding Alembic transaction must roll back and restore the role.
    # Executing RESET ROLE in an aborted transaction would mask the original
    # PostgreSQL error with InFailedSQLTransactionError.
    for statement in statements:
        bind.execute(sa.text(statement))
    bind.execute(sa.text("RESET ROLE"))
    _rb1m2a_require_migration_owner(bind)


def _rb1m2a_direct_private_create_acl_rows(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                acl_data.grantor::text AS grantor_oid,
                grantor_role.rolname::text AS grantor_name,
                acl_data.grantee::text AS grantee_oid,
                grantee_role.rolname::text AS grantee_name,
                acl_data.privilege_type::text AS privilege_type,
                acl_data.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                namespace_data.nspacl
            ) AS acl_data
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl_data.grantee
            WHERE namespace_data.nspname = :schema_name
              AND grantee_role.rolname = :grantee_name
              AND acl_data.privilege_type = 'CREATE'
            ORDER BY
                acl_data.grantor,
                acl_data.grantee,
                acl_data.privilege_type,
                acl_data.is_grantable
            """
        ),
        {
            "schema_name": _RB1M2A_PRIVATE_SCHEMA,
            "grantee_name": _RB1M2A_TARGET_OWNER,
        },
    ).mappings().all()
    result = []
    for row in rows:
        if row["grantor_name"] is None or row["grantee_name"] is None:
            raise RuntimeError("RB1M2A encountered an ACL row with an unknown role.")
        result.append(
            (
                row["grantor_oid"],
                row["grantor_name"],
                row["grantee_oid"],
                row["grantee_name"],
                row["privilege_type"],
                bool(row["is_grantable"]),
            )
        )
    return tuple(result)


def _rb1m2a_verify_private_create_acl(bind, expected, stage):
    observed = tuple(sorted(_rb1m2a_direct_private_create_acl_rows(bind)))
    expected = tuple(sorted(expected))
    if observed != expected:
        raise RuntimeError(
            f"RB1M2A app_private CREATE ACL drift at {stage}: "
            f"observed={observed!r}, expected={expected!r}."
        )


def _rb1m2a_direct_select_acl_rows(bind, schema_name, relation_name):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                acl_data.grantor::text AS grantor_oid,
                grantor_role.rolname::text AS grantor_name,
                acl_data.grantee::text AS grantee_oid,
                grantee_role.rolname::text AS grantee_name,
                acl_data.privilege_type::text AS privilege_type,
                acl_data.is_grantable AS is_grantable
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                relation_data.relacl
            ) AS acl_data
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl_data.grantee
            WHERE namespace_data.nspname = :schema_name
              AND relation_data.relname = :relation_name
              AND grantee_role.rolname = :grantee_name
              AND acl_data.privilege_type = 'SELECT'
            ORDER BY
                acl_data.grantor,
                acl_data.grantee,
                acl_data.privilege_type,
                acl_data.is_grantable
            """
        ),
        {
            "schema_name": schema_name,
            "relation_name": relation_name,
            "grantee_name": _RB1M2A_TARGET_OWNER,
        },
    ).mappings().all()
    result = []
    for row in rows:
        if row["grantor_name"] is None or row["grantee_name"] is None:
            raise RuntimeError(
                "RB1M2A encountered a relation ACL row with an unknown role."
            )
        result.append(
            (
                row["grantor_oid"],
                row["grantor_name"],
                row["grantee_oid"],
                row["grantee_name"],
                row["privilege_type"],
                bool(row["is_grantable"]),
            )
        )
    return tuple(result)


def _rb1m2a_relation_owner(bind, schema_name, relation_name):
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.relkind::text AS relation_kind,
                owner_role.rolname::text AS owner_name
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            WHERE namespace_data.nspname = :schema_name
              AND relation_data.relname = :relation_name
            """
        ),
        {"schema_name": schema_name, "relation_name": relation_name},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            f"Required base relation {schema_name}.{relation_name} is absent."
        )
    if row["relation_kind"] not in ("r", "p"):
        raise RuntimeError(
            f"Required base relation {schema_name}.{relation_name} has "
            f"unexpected relkind {row['relation_kind']!r}."
        )
    return row["owner_name"]


def _rb1m2a_require_native_relation_oid(relation_oid, context):
    if isinstance(relation_oid, bool) or not isinstance(relation_oid, int):
        raise RuntimeError(
            f"{context} returned a non-native PostgreSQL relation OID: "
            f"{relation_oid!r} ({type(relation_oid).__name__})."
        )
    if relation_oid <= 0:
        raise RuntimeError(
            f"{context} returned an invalid PostgreSQL relation OID: "
            f"{relation_oid!r}."
        )
    return relation_oid


def _rb1m2a_relation_oid(bind, schema_name, relation_name):
    relation_oid = bind.execute(
        sa.text(
            """
            SELECT relation_data.oid AS relation_oid
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = :schema_name
              AND relation_data.relname = :relation_name
            """
        ),
        {"schema_name": schema_name, "relation_name": relation_name},
    ).scalar_one_or_none()
    if relation_oid is None:
        raise RuntimeError(
            f"Required relation {schema_name}.{relation_name} is absent."
        )
    return _rb1m2a_require_native_relation_oid(
        relation_oid,
        f"Relation lookup for {schema_name}.{relation_name}",
    )


def _rb1m2a_preflight(bind, *, require_objects):
    """Read-only validation before any revision-0025 catalog mutation."""
    _rb1m2a_require_migration_owner(bind)
    schema_rows = bind.execute(
        sa.text(
            """
            SELECT
                requested.schema_name,
                namespace_data.oid IS NOT NULL AS schema_exists,
                owner_role.rolname::text AS owner_name
            FROM (
                VALUES ('app_private'::text), ('app_secure'::text)
            ) AS requested(schema_name)
            LEFT JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.nspname = requested.schema_name
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = namespace_data.nspowner
            ORDER BY requested.schema_name
            """
        )
    ).mappings().all()
    schemas = {row["schema_name"]: row for row in schema_rows}
    if not schemas[_RB1M2A_PRIVATE_SCHEMA]["schema_exists"]:
        raise RuntimeError("Required schema app_private is absent.")
    if schemas[_RB1M2A_PRIVATE_SCHEMA]["owner_name"] != "migration_owner":
        raise RuntimeError(
            "app_private must remain owned by migration_owner; observed "
            f"{schemas[_RB1M2A_PRIVATE_SCHEMA]['owner_name']!r}."
        )
    if not schemas[_RB1M2A_SECURE_SCHEMA]["schema_exists"]:
        raise RuntimeError("Required schema app_secure is absent.")
    if schemas[_RB1M2A_SECURE_SCHEMA]["owner_name"] != _RB1M2A_TARGET_OWNER:
        raise RuntimeError(
            "app_secure must be owned by app_security_owner; observed "
            f"{schemas[_RB1M2A_SECURE_SCHEMA]['owner_name']!r}."
        )

    role_exists = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = :target_owner
            )
            """
        ),
        {"target_owner": _RB1M2A_TARGET_OWNER},
    ).scalar_one()
    if role_exists is not True:
        raise RuntimeError("Required managed role app_security_owner is absent.")
    if not _rb1m2a_can_set_role(bind, _RB1M2A_TARGET_OWNER):
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner.")
    if not _rb1m2a_has_schema_privilege(
        bind, "migration_owner", _RB1M2A_PRIVATE_SCHEMA, "CREATE"
    ):
        raise RuntimeError("migration_owner lacks CREATE on app_private.")
    if not _rb1m2a_has_schema_privilege(
        bind, "migration_owner", _RB1M2A_PRIVATE_SCHEMA, "USAGE"
    ):
        raise RuntimeError("migration_owner lacks USAGE on app_private.")
    if not _rb1m2a_has_schema_privilege(
        bind, _RB1M2A_TARGET_OWNER, _RB1M2A_PRIVATE_SCHEMA, "USAGE"
    ):
        raise RuntimeError("app_security_owner lacks USAGE on app_private.")
    if not _rb1m2a_has_schema_privilege(
        bind, _RB1M2A_TARGET_OWNER, "public", "USAGE"
    ):
        raise RuntimeError("app_security_owner lacks USAGE on public.")
    public_create = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace_data
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    namespace_data.nspacl
                ) AS acl_data
                WHERE namespace_data.nspname = 'app_private'
                  AND acl_data.grantee = 0
                  AND acl_data.privilege_type = 'CREATE'
            )
            """
        )
    ).scalar_one()
    if public_create is True:
        raise RuntimeError("PUBLIC CREATE on app_private is forbidden.")
    _rb1m2a_direct_private_create_acl_rows(bind)

    function_rows = bind.execute(
        sa.text(
            """
            SELECT
                requested.signature,
                procedure_data.oid IS NOT NULL AS function_exists,
                procedure_data.prokind::text AS function_kind,
                owner_role.rolname::text AS owner_name
            FROM (
                VALUES
                    ('app_private.validate_effective_from_window()'::text),
                    ('app_private.validate_rls_context_match()'::text)
            ) AS requested(signature)
            LEFT JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = pg_catalog.to_regprocedure(
                    requested.signature
                 )
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            ORDER BY requested.signature
            """
        )
    ).mappings().all()
    if len(function_rows) != 2:
        raise RuntimeError("Revision-0025 function preflight returned drift.")
    for row in function_rows:
        if row["function_exists"]:
            if row["function_kind"] != "f":
                raise RuntimeError(
                    f"Pre-existing target routine has incompatible kind: {row!r}."
                )
            if row["owner_name"] != _RB1M2A_TARGET_OWNER:
                raise RuntimeError(
                    "Pre-existing revision-0025 function has unauthorized owner: "
                    f"{row!r}."
                )
        elif require_objects:
            raise RuntimeError(
                "Required revision-0025 function is absent during downgrade: "
                f"{row['signature']}."
            )

    view_row = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.oid IS NOT NULL AS view_exists,
                relation_data.relkind::text AS relation_kind,
                owner_role.rolname::text AS owner_name
            FROM (SELECT 1) AS singleton
            LEFT JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.nspname = 'app_secure'
            LEFT JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.relnamespace = namespace_data.oid
             AND relation_data.relname = 'v_active_branch_staff_roles'
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            """
        )
    ).mappings().one()
    if view_row["view_exists"]:
        if view_row["relation_kind"] != "v":
            raise RuntimeError(
                "Existing app_secure.v_active_branch_staff_roles is not a normal view."
            )
        if view_row["owner_name"] != _RB1M2A_TARGET_OWNER:
            raise RuntimeError(
                "Existing app_secure.v_active_branch_staff_roles has "
                f"unauthorized owner {view_row['owner_name']!r}."
            )
    elif require_objects:
        raise RuntimeError(
            "Required app_secure.v_active_branch_staff_roles is absent during downgrade."
        )

    state_exists = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.migration_0025_view_acl_state'
            ) IS NOT NULL
            """
        )
    ).scalar_one()
    if require_objects and state_exists is not True:
        raise RuntimeError("Revision-0025 view ACL state table is absent.")
    if not require_objects and state_exists is True:
        raise RuntimeError("Unexpected pre-existing revision-0025 ACL state table.")
    if require_objects:
        _rb1m2a_validate_view_acl_state(bind)
        _rb1m2a_verify_function_contracts(bind)

    for schema_name, relation_name in _RB1M2A_BASE_RELATIONS:
        owner_name = _rb1m2a_relation_owner(bind, schema_name, relation_name)
        _rb1m2a_direct_select_acl_rows(bind, schema_name, relation_name)
        if owner_name not in _RB1M2A_ALLOWED_SET_ROLES:
            raise RuntimeError(
                f"Unapproved base-relation owner {owner_name!r} for "
                f"{schema_name}.{relation_name}."
            )
        if owner_name != "migration_owner" and not _rb1m2a_can_set_role(
            bind, owner_name
        ):
            raise RuntimeError(
                f"migration_owner cannot SET ROLE owner {owner_name!r} for "
                f"{schema_name}.{relation_name}."
            )


def _rb1m2a_validate_view_acl_state(bind):
    """Read-only proof of the captured base-relation ACL contract."""
    _rb1m2a_require_migration_owner(bind)
    state_relation = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.relkind::text AS relation_kind,
                owner_role.rolname::text AS owner_name
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            WHERE namespace_data.nspname = 'app_private'
              AND relation_data.relname = 'migration_0025_view_acl_state'
            """
        )
    ).mappings().one_or_none()
    if state_relation is None:
        raise RuntimeError("Revision-0025 view ACL state table is absent.")
    if state_relation["relation_kind"] != "r":
        raise RuntimeError(
            "Revision-0025 view ACL state object is not a normal table."
        )
    if state_relation["owner_name"] != "migration_owner":
        raise RuntimeError(
            "Revision-0025 view ACL state table has unauthorized owner "
            f"{state_relation['owner_name']!r}."
        )

    rows = bind.execute(
        sa.text(
            """
            SELECT
                relation_schema,
                relation_name,
                relation_owner_name,
                prestate_json::text AS prestate_json_text,
                added_by_revision,
                added_grantor_name
            FROM app_private.migration_0025_view_acl_state
            ORDER BY relation_schema, relation_name
            """
        )
    ).mappings().all()
    expected_relations = tuple(sorted(_RB1M2A_BASE_RELATIONS))
    observed_relations = tuple(
        (row["relation_schema"], row["relation_name"])
        for row in rows
    )
    if observed_relations != expected_relations:
        raise RuntimeError(
            "Revision-0025 view ACL state relation-set drift: "
            f"observed={observed_relations!r}, expected={expected_relations!r}."
        )

    target_owner_oid = bind.execute(
        sa.text("SELECT 'app_security_owner'::regrole::oid::text")
    ).scalar_one()
    for row in rows:
        schema_name = row["relation_schema"]
        relation_name = row["relation_name"]
        qualified = f"{schema_name}.{relation_name}"
        current_owner = _rb1m2a_relation_owner(
            bind, schema_name, relation_name
        )
        if current_owner != row["relation_owner_name"]:
            raise RuntimeError(
                f"Recorded owner drift for {qualified}: "
                f"recorded={row['relation_owner_name']!r}, "
                f"observed={current_owner!r}."
            )
        try:
            raw_prestate = json.loads(row["prestate_json_text"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid ACL prestate JSON for {qualified}: "
                f"{row['prestate_json_text']!r}."
            ) from exc
        if not isinstance(raw_prestate, list):
            raise RuntimeError(
                f"Invalid ACL prestate JSON for {qualified}: {raw_prestate!r}."
            )
        prestate = []
        for item in raw_prestate:
            if not isinstance(item, list) or len(item) != 6:
                raise RuntimeError(
                    f"Invalid ACL tuple for {qualified}: {item!r}."
                )
            acl_row = tuple(item)
            if (
                not all(isinstance(acl_row[index], str) for index in range(5))
                or not acl_row[0]
                or not acl_row[1]
                or acl_row[2] != target_owner_oid
                or acl_row[3] != _RB1M2A_TARGET_OWNER
                or acl_row[4] != "SELECT"
                or not isinstance(acl_row[5], bool)
            ):
                raise RuntimeError(
                    f"Invalid captured SELECT ACL tuple for {qualified}: "
                    f"{acl_row!r}."
                )
            prestate.append(acl_row)
        prestate = tuple(sorted(prestate))
        expected_current = prestate
        if row["added_by_revision"]:
            grantor_name = row["added_grantor_name"]
            if grantor_name != row["relation_owner_name"]:
                raise RuntimeError(
                    f"Recorded revision-added grantor drift for {qualified}."
                )
            grantor_oid = bind.execute(
                sa.text(
                    "SELECT pg_catalog.to_regrole("
                    "CAST(:role_name AS text)"
                    ")::oid::text"
                ),
                {"role_name": grantor_name},
            ).scalar_one()
            expected_current = tuple(
                sorted(
                    prestate
                    + (
                        (
                            grantor_oid,
                            grantor_name,
                            target_owner_oid,
                            _RB1M2A_TARGET_OWNER,
                            "SELECT",
                            False,
                        ),
                    )
                )
            )
        elif row["added_grantor_name"] is not None:
            raise RuntimeError(
                f"Unexpected added grantor marker for {qualified}."
            )
        observed = tuple(
            sorted(
                _rb1m2a_direct_select_acl_rows(
                    bind, schema_name, relation_name
                )
            )
        )
        if observed != expected_current:
            raise RuntimeError(
                f"Captured SELECT ACL contract drift for {qualified}: "
                f"observed={observed!r}, expected={expected_current!r}."
            )


def _rb1m2a_prepare_function_owner_transfer(bind):
    _rb1m2a_require_migration_owner(bind)
    before = tuple(sorted(_rb1m2a_direct_private_create_acl_rows(bind)))
    added_create = False
    expected = before
    if not _rb1m2a_has_schema_privilege(
        bind, _RB1M2A_TARGET_OWNER, _RB1M2A_PRIVATE_SCHEMA, "CREATE"
    ):
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_private TO app_security_owner"
            )
        )
        migration_owner_oid = bind.execute(
            sa.text("SELECT 'migration_owner'::regrole::oid::text")
        ).scalar_one()
        target_owner_oid = bind.execute(
            sa.text("SELECT 'app_security_owner'::regrole::oid::text")
        ).scalar_one()
        expected = tuple(
            sorted(
                before
                + (
                    (
                        migration_owner_oid,
                        "migration_owner",
                        target_owner_oid,
                        _RB1M2A_TARGET_OWNER,
                        "CREATE",
                        False,
                    ),
                )
            )
        )
        _rb1m2a_verify_private_create_acl(
            bind, expected, "temporary CREATE grant"
        )
        added_create = True
    if not _rb1m2a_has_schema_privilege(
        bind, _RB1M2A_TARGET_OWNER, _RB1M2A_PRIVATE_SCHEMA, "CREATE"
    ):
        raise RuntimeError(
            "app_security_owner lacks effective CREATE on app_private after preparation."
        )
    return {"before": before, "added_create": added_create}


def _rb1m2a_restore_function_owner_transfer(bind, state):
    _rb1m2a_require_migration_owner(bind)
    if state["added_create"]:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_private FROM app_security_owner"
            )
        )
    _rb1m2a_verify_private_create_acl(
        bind, state["before"], "function owner-transfer restoration"
    )


def _rb1m2a_assert_function_contract(bind, signature, trigger_name):
    row = bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid::regprocedure::text AS signature,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                procedure_data.proisstrict AS is_strict,
                procedure_data.provolatile::text AS volatility,
                procedure_data.proparallel::text AS parallel_mode,
                COALESCE(
                    array_to_string(procedure_data.proconfig, ','),
                    '<NULL>'
                ) AS function_config,
                (
                    SELECT count(*)
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            procedure_data.proacl,
                            pg_catalog.acldefault('f', procedure_data.proowner)
                        )
                    ) AS function_acl
                    WHERE function_acl.grantee = 0
                      AND function_acl.privilege_type = 'EXECUTE'
                ) AS public_execute_count
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Protected function {signature!r} is absent.")
    if row["signature"] != signature:
        raise RuntimeError(f"Function identity drift: {row!r}.")
    if row["owner_name"] != _RB1M2A_TARGET_OWNER:
        raise RuntimeError(f"Function owner drift: {row!r}.")
    if row["security_definer"] is not True:
        raise RuntimeError(f"SECURITY DEFINER drift: {row!r}.")
    if row["is_strict"] is not True:
        raise RuntimeError(f"STRICT drift: {row!r}.")
    if row["volatility"] != "v":
        raise RuntimeError(f"VOLATILE drift: {row!r}.")
    if row["parallel_mode"] != "u":
        raise RuntimeError(f"PARALLEL UNSAFE drift: {row!r}.")
    if row["function_config"] != "search_path=pg_catalog":
        raise RuntimeError(f"Function search_path drift: {row!r}.")
    if int(row["public_execute_count"]) != 0:
        raise RuntimeError(f"PUBLIC EXECUTE drift: {row!r}.")

    trigger_row = bind.execute(
        sa.text(
            """
            SELECT
                trigger_data.tgname::text AS trigger_name,
                procedure_data.oid::regprocedure::text AS signature
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = trigger_data.tgfoid
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = 'branch_staff_roles'
              AND trigger_data.tgname = :trigger_name
            """
        ),
        {"trigger_name": trigger_name},
    ).mappings().one_or_none()
    expected_trigger = {
        "trigger_name": trigger_name,
        "signature": signature,
    }
    if trigger_row is None or dict(trigger_row) != expected_trigger:
        raise RuntimeError(
            "Revision-0025 trigger mapping drift: "
            f"observed={trigger_row!r}, expected={expected_trigger!r}."
        )


def _rb1m2a_verify_function_contracts(bind):
    for trigger_name, signature in _RB1M2A_TRIGGER_MAP:
        _rb1m2a_assert_function_contract(bind, signature, trigger_name)


def _rb1m2a_prepare_view_acl_state(bind):
    _rb1m2a_require_migration_owner(bind)
    bind.execute(
        sa.text(
            """
            CREATE TABLE app_private.migration_0025_view_acl_state (
                relation_schema TEXT NOT NULL,
                relation_name TEXT NOT NULL,
                relation_owner_name TEXT NOT NULL,
                prestate_json JSONB NOT NULL,
                added_by_revision BOOLEAN NOT NULL DEFAULT FALSE,
                added_grantor_name TEXT NULL,
                PRIMARY KEY (relation_schema, relation_name)
            )
            """
        )
    )
    for schema_name, relation_name in _RB1M2A_BASE_RELATIONS:
        owner_name = _rb1m2a_relation_owner(bind, schema_name, relation_name)
        before = tuple(
            sorted(
                _rb1m2a_direct_select_acl_rows(
                    bind, schema_name, relation_name
                )
            )
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.migration_0025_view_acl_state (
                    relation_schema,
                    relation_name,
                    relation_owner_name,
                    prestate_json,
                    added_by_revision,
                    added_grantor_name
                ) VALUES (
                    :relation_schema,
                    :relation_name,
                    :relation_owner_name,
                    CAST(:prestate_json AS jsonb),
                    FALSE,
                    NULL
                )
                """
            ),
            {
                "relation_schema": schema_name,
                "relation_name": relation_name,
                "relation_owner_name": owner_name,
                "prestate_json": json.dumps(before),
            },
        )
        qualified = f"{schema_name}.{relation_name}"
        relation_oid = _rb1m2a_relation_oid(
            bind, schema_name, relation_name
        )
        if not _rb1m2a_has_table_privilege(
            bind, _RB1M2A_TARGET_OWNER, relation_oid, "SELECT"
        ):
            grant_sql = (
                f"GRANT SELECT ON {qualified} TO app_security_owner"
            )
            _rb1m2a_run_as_role(bind, owner_name, grant_sql)
            observed = tuple(
                sorted(
                    _rb1m2a_direct_select_acl_rows(
                        bind, schema_name, relation_name
                    )
                )
            )
            added = [row for row in observed if row not in before]
            if len(added) != 1:
                raise RuntimeError(
                    f"Unexpected SELECT ACL delta for {qualified}: {added!r}."
                )
            added_row = added[0]
            if (
                added_row[1] != owner_name
                or added_row[3] != _RB1M2A_TARGET_OWNER
                or added_row[4] != "SELECT"
                or added_row[5] is not False
            ):
                raise RuntimeError(
                    f"Unexpected revision-added SELECT tuple: {added_row!r}."
                )
            bind.execute(
                sa.text(
                    """
                    UPDATE app_private.migration_0025_view_acl_state
                    SET added_by_revision = TRUE,
                        added_grantor_name = :grantor_name
                    WHERE relation_schema = :relation_schema
                      AND relation_name = :relation_name
                    """
                ),
                {
                    "grantor_name": owner_name,
                    "relation_schema": schema_name,
                    "relation_name": relation_name,
                },
            )
    return True


def _rb1m2a_restore_view_acl_state(bind):
    _rb1m2a_require_migration_owner(bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                relation_schema,
                relation_name,
                relation_owner_name,
                prestate_json::text AS prestate_json_text,
                added_by_revision,
                added_grantor_name
            FROM app_private.migration_0025_view_acl_state
            ORDER BY relation_schema, relation_name
            """
        )
    ).mappings().all()
    if len(rows) != len(_RB1M2A_BASE_RELATIONS):
        raise RuntimeError("Revision-0025 view ACL state row-count drift.")
    for row in rows:
        schema_name = row["relation_schema"]
        relation_name = row["relation_name"]
        qualified = f"{schema_name}.{relation_name}"
        try:
            raw_prestate = json.loads(row["prestate_json_text"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid stored ACL prestate for {qualified}."
            ) from exc
        expected = tuple(tuple(item) for item in raw_prestate)
        if row["added_by_revision"]:
            grantor_name = row["added_grantor_name"]
            if grantor_name != row["relation_owner_name"]:
                raise RuntimeError(
                    f"Recorded grantor drift for {qualified}: {row!r}."
                )
            revoke_sql = (
                f"REVOKE SELECT ON {qualified} FROM app_security_owner"
            )
            _rb1m2a_run_as_role(bind, grantor_name, revoke_sql)
        observed = tuple(
            sorted(
                _rb1m2a_direct_select_acl_rows(
                    bind, schema_name, relation_name
                )
            )
        )
        if observed != tuple(sorted(expected)):
            raise RuntimeError(
                f"Exact SELECT ACL restoration failed for {qualified}: "
                f"observed={observed!r}, expected={expected!r}."
            )
    bind.execute(
        sa.text(
            "DROP TABLE app_private.migration_0025_view_acl_state RESTRICT"
        )
    )


def _rb1m2a_create_secure_view(bind):
    _rb1m2a_require_migration_owner(bind)
    statements = (
        """
        CREATE OR REPLACE VIEW app_secure.v_active_branch_staff_roles
        WITH (security_barrier = true, security_invoker = true)
        AS
        SELECT
            bsr.id,
            bsr.org_id,
            bsr.branch_id,
            bsr.organization_member_id,
            bsr.role_id,
            sr.code          AS role_code,
            sr.hierarchy_level,
            bsr.scope_type_id,
            st.code          AS scope_code,
            bsr.assignment_source,
            bsr.assigned_by,
            bsr.assigned_at,
            bsr.effective_from,
            bsr.effective_to,
            bsr.user_id,
            bsr.role         AS role_legacy,
            bsr.created_at
        FROM public.branch_staff_roles bsr
        LEFT JOIN public.staff_roles  sr ON sr.id = bsr.role_id
        LEFT JOIN public.scope_types  st ON st.id = bsr.scope_type_id
        WHERE bsr.deleted_at IS NULL
          AND bsr.revoked_at IS NULL
        """,
        """
        REVOKE ALL ON app_secure.v_active_branch_staff_roles FROM PUBLIC
        """,
        """
        GRANT SELECT ON app_secure.v_active_branch_staff_roles
            TO app_runtime, readonly_analytics
        """,
        """
        COMMENT ON VIEW app_secure.v_active_branch_staff_roles IS
            'Security-barrier view of active branch staff role assignments. '
            'Joins staff_roles and scope_types for human-readable codes. '
            'Application code should query this view, not the base table directly.'
        """,
    )
    _rb1m2a_run_as_role(bind, _RB1M2A_TARGET_OWNER, statements)
    _rb1m2a_verify_view_contract(bind)


def _rb1m2a_verify_view_contract(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.oid AS relation_oid,
                owner_role.rolname::text AS owner_name,
                relation_data.relkind::text AS relation_kind,
                COALESCE(
                    array_to_string(relation_data.reloptions, ','),
                    '<NULL>'
                ) AS reloptions,
                pg_catalog.obj_description(
                    relation_data.oid,
                    'pg_class'
                )::text AS view_comment,
                (
                    SELECT count(*)
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            relation_data.relacl,
                            pg_catalog.acldefault('r', relation_data.relowner)
                        )
                    ) AS view_acl
                    WHERE view_acl.grantee = 0
                ) AS public_acl_count
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = relation_data.relowner
            WHERE namespace_data.nspname = 'app_secure'
              AND relation_data.relname = 'v_active_branch_staff_roles'
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            "Protected view app_secure.v_active_branch_staff_roles is absent "
            "during contract verification."
        )
    view_relation_oid = _rb1m2a_require_native_relation_oid(
        row["relation_oid"],
        "Protected view app_secure.v_active_branch_staff_roles lookup",
    )
    if row["owner_name"] != _RB1M2A_TARGET_OWNER:
        raise RuntimeError(f"View owner drift: {row!r}.")
    if row["relation_kind"] != "v":
        raise RuntimeError(f"View kind drift: {row!r}.")
    options = set(row["reloptions"].split(","))
    if options != {"security_barrier=true", "security_invoker=true"}:
        raise RuntimeError(f"View reloptions drift: {row!r}.")
    expected_comment = (
        "Security-barrier view of active branch staff role assignments. "
        "Joins staff_roles and scope_types for human-readable codes. "
        "Application code should query this view, not the base table directly."
    )
    if row["view_comment"] != expected_comment:
        raise RuntimeError(f"View comment drift: {row!r}.")
    if int(row["public_acl_count"]) != 0:
        raise RuntimeError(f"PUBLIC view ACL drift: {row!r}.")
    acl_rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                CASE
                    WHEN view_acl.grantee = 0 THEN 'PUBLIC'
                    ELSE grantee_role.rolname::text
                END AS grantee_name,
                view_acl.privilege_type::text AS privilege_type,
                view_acl.is_grantable AS is_grantable
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                relation_data.relacl
            ) AS view_acl
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = view_acl.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = view_acl.grantee
            WHERE namespace_data.nspname = 'app_secure'
              AND relation_data.relname = 'v_active_branch_staff_roles'
              AND view_acl.grantee <> relation_data.relowner
            ORDER BY
                grantee_name, privilege_type, is_grantable, grantor_name
            """
        )
    ).all()
    observed_acl = tuple(
        (item[0], item[1], item[2], bool(item[3])) for item in acl_rows
    )
    expected_acl = (
        (
            _RB1M2A_TARGET_OWNER,
            "app_runtime",
            "SELECT",
            False,
        ),
        (
            _RB1M2A_TARGET_OWNER,
            "readonly_analytics",
            "SELECT",
            False,
        ),
    )
    if observed_acl != expected_acl:
        raise RuntimeError(
            "Unexpected view ACL contract: "
            f"observed={observed_acl!r}, expected={expected_acl!r}."
        )
    for grantee in ("app_runtime", "readonly_analytics"):
        if not _rb1m2a_has_table_privilege(
            bind, grantee, view_relation_oid, "SELECT"
        ):
            raise RuntimeError(f"{grantee} lacks SELECT on {_RB1M2A_VIEW}.")

def _rb1m2a_drop_secure_view(bind):
    _rb1m2a_run_as_role(
        bind,
        _RB1M2A_TARGET_OWNER,
        "DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT",
    )


def _rb1m2a_drop_owned_function(bind, signature):
    if signature not in _RB1M2A_FUNCTIONS:
        raise RuntimeError(f"Unapproved revision-0025 function {signature!r}.")
    _rb1m2a_run_as_role(
        bind,
        _RB1M2A_TARGET_OWNER,
        f"DROP FUNCTION {signature} RESTRICT",
    )
# RB1M2A_0025_COMPLETE_OWNER_CONTEXT_HELPERS_END



# RB1M2U_0025_SYNC_CONTRACT_HELPER_START
def _rb1m2u_assert_sync_contract(bind):
    """Fail-closed proof for the expand-phase dual-write synchronizer."""
    _rb1m2a_require_migration_owner(bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid::text AS function_oid,
                procedure_data.prokind::text AS function_kind,
                procedure_data.prosecdef AS security_definer,
                owner_role.rolname::text AS owner_name,
                trigger_data.oid IS NOT NULL AS trigger_exists,
                trigger_data.tgenabled::text AS trigger_enabled,
                trigger_data.tgisinternal AS trigger_internal,
                (trigger_data.tgfoid = procedure_data.oid)
                    AS trigger_matches_function
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            LEFT JOIN pg_catalog.pg_trigger AS trigger_data
              ON trigger_data.tgrelid = 'public.branch_staff_roles'::regclass
             AND trigger_data.tgname =
                    'trg_sync_branch_staff_role_contract_fields'
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(
                'app_private.sync_branch_staff_role_contract_fields()'
            )
            """
        )
    ).mappings().all()

    if len(rows) != 1:
        raise RuntimeError(
            "0025 dual-write synchronizer function is absent or ambiguous."
        )
    row = rows[0]
    if row["function_kind"] != "f":
        raise RuntimeError(
            f"0025 synchronizer has incompatible routine kind: {row!r}."
        )
    if row["owner_name"] != "migration_owner":
        raise RuntimeError(
            f"0025 synchronizer owner drift: {row['owner_name']!r}."
        )
    if row["security_definer"] is not False:
        raise RuntimeError(
            "0025 synchronizer must remain SECURITY INVOKER."
        )
    if (
        row["trigger_exists"] is not True
        or row["trigger_enabled"] != "O"
        or row["trigger_internal"] is not False
        or row["trigger_matches_function"] is not True
    ):
        raise RuntimeError(
            f"0025 synchronizer trigger mapping drift: {row!r}."
        )

    execute_acl_rows = bind.execute(
        sa.text(
            """
            SELECT
                acl_data.grantor::oid AS grantor_oid,
                grantor_role.rolname::text AS grantor_name,
                acl_data.grantee::oid AS grantee_oid,
                CASE
                    WHEN acl_data.grantee = 0 THEN 'PUBLIC'
                    ELSE grantee_role.rolname::text
                END AS grantee_name,
                acl_data.privilege_type::text AS privilege_type,
                acl_data.is_grantable
            FROM pg_catalog.pg_proc AS procedure_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure_data.proacl,
                    pg_catalog.acldefault(
                        'f'::"char",
                        procedure_data.proowner
                    )
                )
            ) AS acl_data
            LEFT JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl_data.grantor
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl_data.grantee
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(
                'app_private.sync_branch_staff_role_contract_fields()'
            )
              AND acl_data.privilege_type = 'EXECUTE'
            ORDER BY
                acl_data.grantee,
                acl_data.grantor,
                acl_data.is_grantable
            """
        )
    ).mappings().all()

    if any(
        acl_row["grantee_oid"] == 0
        for acl_row in execute_acl_rows
    ):
        raise RuntimeError(
            "PUBLIC EXECUTE on the 0025 synchronizer is forbidden."
        )

    allowed_execute_grantees = {
        "migration_owner",
        "app_runtime",
        "app_rls_executor",
    }
    unexpected_execute_grantees = sorted(
        {
            acl_row["grantee_name"]
            for acl_row in execute_acl_rows
            if acl_row["grantee_oid"] != 0
            and acl_row["grantee_name"] not in allowed_execute_grantees
        }
    )
    if unexpected_execute_grantees:
        raise RuntimeError(
            "0025 synchronizer has unexpected direct EXECUTE ACL grantees: "
            f"{unexpected_execute_grantees!r}."
        )

    for grantee in ("app_runtime", "app_rls_executor"):
        grantee_rows = [
            acl_row
            for acl_row in execute_acl_rows
            if acl_row["grantee_name"] == grantee
        ]
        if not grantee_rows:
            raise RuntimeError(
                f"{grantee} lacks direct EXECUTE on the 0025 synchronizer."
            )
        if any(
            acl_row["grantor_name"] != "migration_owner"
            for acl_row in grantee_rows
        ):
            raise RuntimeError(
                f"{grantee} EXECUTE on the 0025 synchronizer has "
                "an unexpected grantor."
            )
        if any(
            acl_row["is_grantable"] is True
            for acl_row in grantee_rows
        ):
            raise RuntimeError(
                f"{grantee} EXECUTE on the 0025 synchronizer must not "
                "carry grant option."
            )
# RB1M2U_0025_SYNC_CONTRACT_HELPER_END


def upgrade() -> None:

    bind = op.get_bind()
    _rb1m2a_preflight(bind, require_objects=False)

    # ── 1. Add new columns ────────────────────────────────────────────────

    # organization_member_id: the new tenancy-scoped actor.
    # NULL during dual-write phase; set NOT NULL in Phase 8 (Contract).
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS organization_member_id UUID NULL;
    """)

    op.execute("""
        COMMENT ON COLUMN public.branch_staff_roles.organization_member_id IS
            'Tenancy-scoped actor reference (v18 model). '
            'NULL during expand/dual-write phase. '
            'Set NOT NULL in Phase 8 (contract step) after full backfill.';
    """)

    # role_id: integer FK replacing the ENUM role column.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS role_id SMALLINT NULL;
    """)

    op.execute("""
        COMMENT ON COLUMN public.branch_staff_roles.role_id IS
            'Integer role reference (replaces role ENUM). '
            'NULL during dual-write phase. Set NOT NULL in Phase 8.';
    """)

    # scope_type_id: defaults to branch scope (id=2).
    # NOT NULL with default — safe to add immediately.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS scope_type_id SMALLINT NOT NULL DEFAULT 2;
    """)

    # assignment_source: audit trail for how the assignment was made.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS assignment_source VARCHAR(32) NOT NULL DEFAULT 'dashboard';
    """)

    # RB1M2U_0025_EXPAND_BACKFILL_START
    # Populate the new canonical representation and keep it synchronized with
    # the legacy representation until the contract revision removes it.
    op.execute("""
        DO $rb1m2u_0025_preflight$
        DECLARE
            v_name TEXT;
            v_owner TEXT;
            v_rls BOOLEAN;
            v_force BOOLEAN;
        BEGIN
            IF session_user <> 'migration_owner'
               OR current_user <> 'migration_owner'
            THEN
                RAISE EXCEPTION
                    '0025 expand backfill requires migration_owner'
                    USING ERRCODE = '42501';
            END IF;

            FOREACH v_name IN ARRAY ARRAY[
                'branch_staff_roles',
                'organization_members'
            ]
            LOOP
                SELECT
                    pg_catalog.pg_get_userbyid(c.relowner),
                    c.relrowsecurity,
                    c.relforcerowsecurity
                INTO v_owner, v_rls, v_force
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = v_name;

                IF v_owner IS DISTINCT FROM 'migration_owner'
                   OR v_rls IS NOT TRUE
                   OR v_force IS NOT TRUE
                THEN
                    RAISE EXCEPTION
                        '0025 predecessor security contract drift for %: '
                        'owner=%, rls=%, force=%',
                        v_name, v_owner, v_rls, v_force
                        USING ERRCODE = '42501';
                END IF;
            END LOOP;
        END
        $rb1m2u_0025_preflight$;
    """)

    op.execute(
        "LOCK TABLE public.organization_members "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "LOCK TABLE public.branch_staff_roles "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute("LOCK TABLE public.staff_roles IN SHARE MODE;")

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.organization_members "
        "NO FORCE ROW LEVEL SECURITY;"
    )

    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET organization_member_id = om.id
        FROM public.organization_members AS om
        WHERE bsr.organization_member_id IS NULL
          AND bsr.org_id = om.org_id
          AND bsr.user_id = om.user_id;
    """)

    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET role_id = sr.id
        FROM public.staff_roles AS sr
        WHERE bsr.role_id IS NULL
          AND sr.code = bsr.role::text;
    """)

    op.execute("""
        DO $rb1m2u_0025_verify$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS om
                  ON om.id = bsr.organization_member_id
                 AND om.org_id = bsr.org_id
                LEFT JOIN public.staff_roles AS sr
                  ON sr.id = bsr.role_id
                WHERE bsr.organization_member_id IS NULL
                   OR bsr.role_id IS NULL
                   OR om.id IS NULL
                   OR om.user_id IS DISTINCT FROM bsr.user_id
                   OR sr.id IS NULL
                   OR sr.code IS DISTINCT FROM bsr.role::text
            ) THEN
                RAISE EXCEPTION
                    '0025 expand backfill left unresolved or inconsistent rows';
            END IF;
        END
        $rb1m2u_0025_verify$;
    """)

    op.execute(
        "ALTER TABLE public.organization_members "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "FORCE ROW LEVEL SECURITY;"
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION
            app_private.sync_branch_staff_role_contract_fields()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path TO 'pg_catalog'
        AS $function$
        DECLARE
            v_member_id UUID;
            v_user_id UUID;
            v_role_id SMALLINT;
            v_role_code TEXT;
        BEGIN
            IF NEW.organization_member_id IS NULL THEN
                SELECT om.id
                INTO STRICT v_member_id
                FROM public.organization_members AS om
                WHERE om.org_id = NEW.org_id
                  AND om.user_id = NEW.user_id;

                NEW.organization_member_id := v_member_id;
            ELSE
                SELECT om.user_id
                INTO STRICT v_user_id
                FROM public.organization_members AS om
                WHERE om.id = NEW.organization_member_id
                  AND om.org_id = NEW.org_id;

                IF NEW.user_id IS NULL THEN
                    NEW.user_id := v_user_id;
                ELSIF NEW.user_id IS DISTINCT FROM v_user_id THEN
                    RAISE EXCEPTION
                        'branch_staff_roles user/member identity mismatch'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            IF NEW.role_id IS NULL THEN
                SELECT sr.id
                INTO STRICT v_role_id
                FROM public.staff_roles AS sr
                WHERE sr.code = NEW.role::text;

                NEW.role_id := v_role_id;
            ELSE
                SELECT sr.code
                INTO STRICT v_role_code
                FROM public.staff_roles AS sr
                WHERE sr.id = NEW.role_id;

                IF NEW.role IS NULL THEN
                    NEW.role := v_role_code::public.branch_staff_role_enum;
                ELSIF NEW.role::text IS DISTINCT FROM v_role_code THEN
                    RAISE EXCEPTION
                        'branch_staff_roles legacy/canonical role mismatch'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $function$;
    """)

    op.execute("""
        REVOKE ALL ON FUNCTION
            app_private.sync_branch_staff_role_contract_fields()
        FROM PUBLIC;
    """)
    op.execute("""
        GRANT EXECUTE ON FUNCTION
            app_private.sync_branch_staff_role_contract_fields()
        TO app_runtime, app_rls_executor;
    """)
    op.execute("""
        CREATE TRIGGER trg_sync_branch_staff_role_contract_fields
        BEFORE INSERT OR UPDATE
        ON public.branch_staff_roles
        FOR EACH ROW
        EXECUTE FUNCTION
            app_private.sync_branch_staff_role_contract_fields();
    """)
    _rb1m2u_assert_sync_contract(bind)
    # RB1M2U_0025_EXPAND_BACKFILL_END

    # ── 2. FK constraints — NOT VALID (no full table scan lock) ──────────
    # Existing rows are NOT validated. Application must backfill before
    # running VALIDATE CONSTRAINT in the post-deploy step.

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_member_id
            FOREIGN KEY (organization_member_id)
            REFERENCES public.organization_members(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    # Composite FK: guarantees (organization_member_id, org_id) cannot
    # reference a member from a different org — prevents cross-tenant corruption.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_member_org
            FOREIGN KEY (organization_member_id, org_id)
            REFERENCES public.organization_members(id, org_id)
            NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_role_id
            FOREIGN KEY (role_id)
            REFERENCES public.staff_roles(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_scope_type_id
            FOREIGN KEY (scope_type_id)
            REFERENCES public.scope_types(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    # ── 3. CHECK constraints ──────────────────────────────────────────────

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_assignment_src
            CHECK (assignment_source IN (
                'dashboard', 'api', 'migration', 'bulk_import', 'automation', 'sync_worker'
            ));
    """)

    # Revocation must not predate the start of the assignment
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_revocation_from
            CHECK (revoked_at IS NULL OR revoked_at >= effective_from);
    """)

    # Revocation must not postdate the scheduled end of the assignment
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_revocation_to
            CHECK (
                effective_to IS NULL
                OR revoked_at IS NULL
                OR revoked_at <= effective_to
            );
    """)

    # ── 4. Temporal exclusion constraint (new model rows only) ────────────
    # Scoped to rows where organization_member_id IS NOT NULL.
    # DEFERRABLE INITIALLY IMMEDIATE allows transactional owner swaps
    # (SET CONSTRAINTS ex_branch_role_overlap_v2 DEFERRED within a txn).
    # btree_gist already installed in Phase 1.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT ex_branch_role_overlap_v2
        EXCLUDE USING gist (
            organization_member_id  WITH =,
            branch_id               WITH =,
            role_id                 WITH =,
            tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz)) WITH &&
        )
        WHERE (
            organization_member_id IS NOT NULL
            AND role_id IS NOT NULL
            AND revoked_at IS NULL
            AND deleted_at IS NULL
        )
        DEFERRABLE INITIALLY IMMEDIATE;
    """)

    # ── 5. Single active owner constraint ─────────────────────────────────
    # Only one active owner (role_id=1) per org at any time.
    # Partial unique index — only covers rows using the new model.
    op.execute("""
        CREATE UNIQUE INDEX uq_bsr_single_owner_per_org
        ON public.branch_staff_roles(org_id)
        WHERE (
            role_id = 1
            AND revoked_at IS NULL
            AND deleted_at IS NULL
            AND organization_member_id IS NOT NULL
        );
    """)

    # ── 6. Active lookup index for new model ──────────────────────────────
    op.execute("""
        CREATE INDEX ix_bsr_member_active
        ON public.branch_staff_roles(org_id, branch_id, organization_member_id)
        WHERE (
            organization_member_id IS NOT NULL
            AND revoked_at IS NULL
            AND deleted_at IS NULL
        );
    """)

    # ── 7. Scheduling window guard trigger ────────────────────────────────
    # Prevents dormant privilege grants by limiting effective_from to
    # at most 30 days in the future.
    # Uses trigger (not CHECK) because CHECK constraints cannot use
    # volatile functions like clock_timestamp().
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.validate_effective_from_window()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.effective_from > clock_timestamp() + interval '30 days' THEN
                RAISE EXCEPTION
                    'effective_from (%) exceeds the 30-day scheduling window. '
                    'Future-dated assignments beyond 30 days are not permitted.',
                    NEW.effective_from
                USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.validate_effective_from_window() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_bsr_validate_effective_from
            BEFORE INSERT OR UPDATE OF effective_from ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.validate_effective_from_window();
    """)
    function_owner_state = _rb1m2a_prepare_function_owner_transfer(bind)
    op.execute("ALTER FUNCTION app_private.validate_effective_from_window() OWNER TO app_security_owner;")
    _rb1m2a_assert_function_contract(
        bind,
        "app_private.validate_effective_from_window()",
        "trg_bsr_validate_effective_from",
    )

    # ── 8. RLS context validation trigger ────────────────────────────────
    # Guards against payload org_id poisoning — the org_id in the row
    # being inserted/updated must match the active tenant GUC.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.validate_rls_context_match()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_current_org_id UUID;
        BEGIN
            BEGIN
                v_current_org_id := current_setting('app.current_org_id', false)::uuid;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION
                    'Security context error: app.current_org_id GUC is not set. '
                    'All tenant operations require an active org context.'
                USING ERRCODE = 'insufficient_privilege';
            END;

            IF NEW.org_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION
                    'Security policy violation: row org_id (%) does not match '
                    'active tenant context (%). Cross-tenant write attempt blocked.',
                    NEW.org_id, v_current_org_id
                USING ERRCODE = 'insufficient_privilege';
            END IF;

            RETURN NEW;
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.validate_rls_context_match() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_bsr_validate_rls_context
            BEFORE INSERT OR UPDATE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.validate_rls_context_match();
    """)
    if not _rb1m2a_has_schema_privilege(
        bind,
        _RB1M2A_TARGET_OWNER,
        _RB1M2A_PRIVATE_SCHEMA,
        "CREATE",
    ):
        raise RuntimeError(
            "app_security_owner lost CREATE on app_private before second transfer."
        )
    op.execute("ALTER FUNCTION app_private.validate_rls_context_match() OWNER TO app_security_owner;")
    _rb1m2a_assert_function_contract(
        bind,
        "app_private.validate_rls_context_match()",
        "trg_bsr_validate_rls_context",
    )
    _rb1m2a_restore_function_owner_transfer(bind, function_owner_state)
    _rb1m2a_verify_function_contracts(bind)

    # ── 9. Harden RLS policy ─────────────────────────────────────────────
    # Drop the old permissive policy and replace with hardened version.
    # Key changes:
    #   • current_setting(..., false) — raises error if GUC not set (fail-closed)
    #   • deleted_at IS NULL enforced in both USING and WITH CHECK
    #   • app.can_read_staff_roles GUC pre-authorization (set per-transaction by app)
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")

    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles
        ON public.branch_staff_roles
        FOR ALL
        USING (
            org_id = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
            AND COALESCE(current_setting('app.can_read_staff_roles', true), 'false') = 'true'
        )
        WITH CHECK (
            org_id = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
        );
    """)

    # Ensure FORCE RLS is still active (survives policy replacement)
    op.execute("ALTER TABLE public.branch_staff_roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles FORCE ROW LEVEL SECURITY;")

    # ── 10. Security barrier view ─────────────────────────────────────────
    # Lives in app_secure schema (not public) to isolate from broad grants.
    # Filters deleted + revoked rows centrally — application queries this view.
    _rb1m2a_prepare_view_acl_state(bind)
    _rb1m2a_create_secure_view(bind)

    # ── 11. Grants on new columns / table ─────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.branch_staff_roles
        TO app_runtime;
    """)
    op.execute("GRANT SELECT ON public.branch_staff_roles TO audit_writer, readonly_analytics;")


def downgrade() -> None:
    bind = op.get_bind()
    _rb1m2a_preflight(bind, require_objects=True)
    _rb1m2u_assert_sync_contract(bind)

    # View and exact revision-added base-relation SELECT ACL restoration.
    _rb1m2a_drop_secure_view(bind)
    _rb1m2a_restore_view_acl_state(bind)

    # RB1M2U_0025_EXPAND_DOWNGRADE_CLEANUP
    op.execute(
        "DROP TRIGGER trg_sync_branch_staff_role_contract_fields "
        "ON public.branch_staff_roles;"
    )
    _rb1m2a_run_as_role(
        bind,
        "migration_owner",
        "DROP FUNCTION app_private.sync_branch_staff_role_contract_fields() RESTRICT",
    )

    # Triggers
    op.execute("DROP TRIGGER IF EXISTS trg_bsr_validate_rls_context ON public.branch_staff_roles;")
    op.execute("DROP TRIGGER IF EXISTS trg_bsr_validate_effective_from ON public.branch_staff_roles;")

    # Trigger functions under bounded actual-owner context.
    _rb1m2a_drop_owned_function(
        bind, "app_private.validate_rls_context_match()"
    )
    _rb1m2a_drop_owned_function(
        bind, "app_private.validate_effective_from_window()"
    )

    # Restore original (weaker) RLS policy
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles ON public.branch_staff_roles
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    # Indexes
    op.execute("DROP INDEX IF EXISTS ix_bsr_member_active;")
    op.execute("DROP INDEX IF EXISTS uq_bsr_single_owner_per_org;")

    # Constraints
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS ex_branch_role_overlap_v2;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_revocation_to;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_revocation_from;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_assignment_src;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_scope_type_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_role_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_member_org;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_member_id;")

    # Columns
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS assignment_source;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS scope_type_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS role_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS organization_member_id;")
