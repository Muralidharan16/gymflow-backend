"""Validate the canonical branch-staff-role deactivation contract.

Revision ID: dafd2b02005e
Revises: b2c3d4e5f6a1
Create Date: 2026-05-23 17:34:34.961439

This historical revision originally attempted to add a second organization-user
deactivation trigger. The predecessor already contains the authoritative
0021/0029 trigger and function, so both migration directions are intentionally
validation-only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dafd2b02005e"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MIGRATION_OWNER = "migration_owner"
_RLS_EXECUTOR = "app_rls_executor"
_PRIVATE_SCHEMA = "app_private"
_CANONICAL_FUNCTION = "handle_user_deactivation_cascade"
_CANONICAL_TRIGGER = "trg_user_deactivation_cascade"
_DUPLICATE_FUNCTION = "handle_org_user_deactivation_cascade"
_DUPLICATE_TRIGGER = "trg_org_user_deactivation_cascade"
_CANONICAL_PROSRC_SHA256 = (
    "42f3cca04ba4b8ca1e1b267eb15e5a5696559014f515af354602cd6315772cab"
)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


def _strip_outer_parentheses(value: str) -> str:
    compact = "".join(value.lower().split())
    while compact.startswith("(") and compact.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(compact):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(compact) - 1:
                    encloses_all = False
                    break
            if depth < 0:
                encloses_all = False
                break
        if not encloses_all or depth != 0:
            break
        compact = compact[1:-1]
    return compact


def _require_execution_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
                current_user::text AS current_name
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "dafd requires session_user migration_owner; observed "
            f"{row['session_name']!r}."
        )
    if row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "dafd requires current_user migration_owner; observed "
            f"{row['current_name']!r}."
        )


def _require_role_contract(bind) -> None:
    migration_role = bind.execute(
        sa.text(
            """
            SELECT
                role.rolsuper AS is_superuser,
                role.rolbypassrls AS bypasses_rls,
                role.rolcreatedb AS can_create_database,
                role.rolcreaterole AS can_create_role,
                role.rolreplication AS can_replicate
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = :migration_owner
            """
        ),
        {"migration_owner": _MIGRATION_OWNER},
    ).mappings().one_or_none()
    if migration_role is None:
        raise RuntimeError("Required migration_owner role is absent.")
    if any(migration_role.values()):
        raise RuntimeError(
            "migration_owner has an unsafe managed-role attribute: "
            f"{dict(migration_role)!r}."
        )

    executor = bind.execute(
        sa.text(
            """
            SELECT
                role.rolcanlogin AS can_login,
                role.rolinherit AS inherits_privileges,
                role.rolsuper AS is_superuser,
                role.rolbypassrls AS bypasses_rls,
                role.rolcreatedb AS can_create_database,
                role.rolcreaterole AS can_create_role,
                role.rolreplication AS can_replicate,
                pg_catalog.pg_has_role(
                    session_user::text,
                    role.rolname::text,
                    'SET'
                ) AS session_can_set
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = :executor
            """
        ),
        {"executor": _RLS_EXECUTOR},
    ).mappings().one_or_none()
    if executor is None:
        raise RuntimeError("Required app_rls_executor role is absent.")
    expected_executor = {
        "can_login": False,
        "inherits_privileges": False,
        "is_superuser": False,
        "bypasses_rls": False,
        "can_create_database": False,
        "can_create_role": False,
        "can_replicate": False,
        "session_can_set": True,
    }
    if dict(executor) != expected_executor:
        raise RuntimeError(
            "app_rls_executor managed-role contract drifted: "
            f"{dict(executor)!r}."
        )

    memberships = bind.execute(
        sa.text(
            """
            SELECT
                grantor.rolname::text AS grantor_name,
                membership.admin_option,
                membership.inherit_option,
                membership.set_option
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_catalog.pg_roles AS member_role
              ON member_role.oid = membership.member
            JOIN pg_catalog.pg_roles AS grantor
              ON grantor.oid = membership.grantor
            WHERE granted_role.rolname = :executor
              AND member_role.rolname = :migration_owner
            ORDER BY grantor.rolname
            """
        ),
        {
            "executor": _RLS_EXECUTOR,
            "migration_owner": _MIGRATION_OWNER,
        },
    ).mappings().all()
    observed_memberships = tuple(
        (
            row["grantor_name"],
            row["admin_option"],
            row["inherit_option"],
            row["set_option"],
        )
        for row in memberships
    )
    if observed_memberships != (("postgres", False, False, True),):
        raise RuntimeError(
            "migration_owner/app_rls_executor membership drifted: "
            f"{observed_memberships!r}."
        )


def _require_private_schema_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                namespace.oid AS schema_oid,
                owner.rolname::text AS owner_name
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_roles AS owner
              ON owner.oid = namespace.nspowner
            WHERE namespace.nspname = :schema_name
            """
        ),
        {"schema_name": _PRIVATE_SCHEMA},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("Required app_private schema is absent.")
    if row["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "app_private owner drifted: " f"{row['owner_name']!r}."
        )

    acl_rows = bind.execute(
        sa.text(
            """
            SELECT
                CASE
                    WHEN acl.grantee = 0 THEN 'PUBLIC'
                    ELSE grantee.rolname::text
                END AS grantee_name,
                acl.privilege_type::text AS privilege_type,
                acl.is_grantable,
                grantor.rolname::text AS grantor_name
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    namespace.nspacl,
                    pg_catalog.acldefault('n', namespace.nspowner)
                )
            ) AS acl
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            JOIN pg_catalog.pg_roles AS grantor
              ON grantor.oid = acl.grantor
            WHERE namespace.oid = :schema_oid
            ORDER BY
                grantee_name,
                privilege_type,
                acl.is_grantable,
                grantor_name
            """
        ),
        {"schema_oid": row["schema_oid"]},
    ).mappings().all()
    observed_acl = tuple(
        (
            acl["grantee_name"],
            acl["privilege_type"],
            acl["is_grantable"],
            acl["grantor_name"],
        )
        for acl in acl_rows
    )
    expected_acl = (
        ("app_rls_executor", "USAGE", False, _MIGRATION_OWNER),
        ("app_runtime", "USAGE", False, _MIGRATION_OWNER),
        ("app_security_owner", "USAGE", False, _MIGRATION_OWNER),
        (_MIGRATION_OWNER, "CREATE", False, _MIGRATION_OWNER),
        (_MIGRATION_OWNER, "USAGE", False, _MIGRATION_OWNER),
    )
    if observed_acl != expected_acl:
        raise RuntimeError(f"app_private ACL drifted: {observed_acl!r}.")


def _require_relation_columns(bind, relation_name, expected_columns) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                attribute.attname::text AS column_name,
                pg_catalog.format_type(
                    attribute.atttypid,
                    attribute.atttypmod
                )::text AS data_type
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = :relation_name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND attribute.attname = ANY(:column_names)
            ORDER BY attribute.attname
            """
        ),
        {
            "relation_name": relation_name,
            "column_names": list(expected_columns),
        },
    ).mappings().all()
    observed = tuple(
        (row["column_name"], row["data_type"]) for row in rows
    )
    expected = tuple(sorted(expected_columns.items()))
    if observed != expected:
        raise RuntimeError(
            f"public.{relation_name} dependency columns drifted: "
            f"{observed!r}."
        )


def _require_function_acl(bind, function_oid) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                CASE
                    WHEN acl.grantee = 0 THEN 'PUBLIC'
                    ELSE grantee.rolname::text
                END AS grantee_name,
                acl.privilege_type::text AS privilege_type,
                acl.is_grantable,
                grantor.rolname::text AS grantor_name
            FROM pg_catalog.pg_proc AS routine
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    routine.proacl,
                    pg_catalog.acldefault('f', routine.proowner)
                )
            ) AS acl
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            JOIN pg_catalog.pg_roles AS grantor
              ON grantor.oid = acl.grantor
            WHERE routine.oid = :function_oid
              AND acl.privilege_type = 'EXECUTE'
            ORDER BY
                grantee_name,
                acl.is_grantable,
                grantor_name
            """
        ),
        {"function_oid": function_oid},
    ).mappings().all()
    observed = tuple(
        (
            row["grantee_name"],
            row["privilege_type"],
            row["is_grantable"],
            row["grantor_name"],
        )
        for row in rows
    )
    expected = ((_RLS_EXECUTOR, "EXECUTE", False, _RLS_EXECUTOR),)
    if observed != expected:
        raise RuntimeError(
            "Canonical deactivation function EXECUTE ACL drifted: "
            f"{observed!r}."
        )


def _require_canonical_function(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                routine.oid AS function_oid,
                pg_catalog.pg_get_function_identity_arguments(
                    routine.oid
                )::text AS identity_arguments,
                owner.rolname::text AS owner_name,
                routine.prokind::text AS routine_kind,
                pg_catalog.format_type(
                    routine.prorettype,
                    NULL
                )::text AS result_type,
                language.lanname::text AS language_name,
                routine.prosecdef AS security_definer,
                routine.proisstrict AS is_strict,
                routine.provolatile::text AS volatility,
                routine.proparallel::text AS parallel_safety,
                routine.proleakproof AS leakproof,
                COALESCE(
                    routine.proconfig,
                    ARRAY[]::text[]
                ) AS config,
                routine.prosrc::text AS source_text,
                pg_catalog.obj_description(
                    routine.oid,
                    'pg_proc'
                )::text AS comment_text
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            JOIN pg_catalog.pg_roles AS owner
              ON owner.oid = routine.proowner
            JOIN pg_catalog.pg_language AS language
              ON language.oid = routine.prolang
            WHERE namespace.nspname = :schema_name
              AND routine.proname = :function_name
            ORDER BY routine.oid
            """
        ),
        {
            "schema_name": _PRIVATE_SCHEMA,
            "function_name": _CANONICAL_FUNCTION,
        },
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError(
            "Expected exactly one canonical deactivation routine; observed "
            f"{len(rows)}."
        )
    row = rows[0]
    expected = {
        "identity_arguments": "",
        "owner_name": _RLS_EXECUTOR,
        "routine_kind": "f",
        "result_type": "trigger",
        "language_name": "plpgsql",
        "security_definer": True,
        "is_strict": False,
        "volatility": "v",
        "parallel_safety": "u",
        "leakproof": False,
        "comment_text": None,
    }
    for key, expected_value in expected.items():
        if row[key] != expected_value:
            raise RuntimeError(
                f"Canonical deactivation function {key} drifted: "
                f"observed={row[key]!r}, expected={expected_value!r}."
            )

    config = tuple(
        sorted(item.replace('"', "") for item in row["config"])
    )
    if config != ("row_security=on", "search_path=pg_catalog"):
        raise RuntimeError(
            "Canonical deactivation function configuration drifted: "
            f"{config!r}."
        )

    source_digest = hashlib.sha256(
        _normalized_sql(row["source_text"]).encode("utf-8")
    ).hexdigest()
    if source_digest != _CANONICAL_PROSRC_SHA256:
        raise RuntimeError(
            "Canonical deactivation function body drifted: "
            f"{source_digest}."
        )

    _require_function_acl(bind, row["function_oid"])
    return row["function_oid"]


def _require_canonical_trigger(bind, function_oid) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                trigger.tgname::text AS trigger_name,
                trigger.tgenabled::text AS enabled_state,
                trigger.tgisinternal AS is_internal,
                trigger.tgconstraint::bigint AS constraint_oid,
                trigger.tgtype::integer AS trigger_type,
                relation_namespace.nspname::text AS relation_schema,
                relation.relname::text AS relation_name,
                relation.relkind::text AS relation_kind,
                relation_owner.rolname::text AS relation_owner,
                relation.relrowsecurity AS rls_enabled,
                relation.relforcerowsecurity AS force_rls,
                (trigger.tgqual IS NOT NULL) AS has_when_predicate,
                pg_catalog.pg_get_triggerdef(
                    trigger.oid,
                    true
                )::text AS trigger_definition,
                ARRAY(
                    SELECT attribute.attname::text
                    FROM pg_catalog.unnest(
                        trigger.tgattr::smallint[]
                    ) WITH ORDINALITY AS update_column(
                        attribute_number,
                        ordinal_position
                    )
                    JOIN pg_catalog.pg_attribute AS attribute
                      ON attribute.attrelid = trigger.tgrelid
                     AND attribute.attnum =
                         update_column.attribute_number
                    ORDER BY update_column.ordinal_position
                ) AS update_columns
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_roles AS relation_owner
              ON relation_owner.oid = relation.relowner
            WHERE trigger.tgfoid = :function_oid
              AND NOT trigger.tgisinternal
            ORDER BY trigger.tgname, relation_namespace.nspname,
                     relation.relname
            """
        ),
        {"function_oid": function_oid},
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError(
            "Expected exactly one trigger invoking the canonical "
            f"deactivation function; observed {len(rows)}."
        )
    row = rows[0]
    expected = {
        "trigger_name": _CANONICAL_TRIGGER,
        "enabled_state": "O",
        "is_internal": False,
        "constraint_oid": 0,
        "trigger_type": 17,
        "relation_schema": "public",
        "relation_name": "organization_users",
        "relation_kind": "r",
        "relation_owner": _MIGRATION_OWNER,
        "rls_enabled": True,
        "force_rls": True,
    }
    for key, expected_value in expected.items():
        if row[key] != expected_value:
            raise RuntimeError(
                f"Canonical deactivation trigger {key} drifted: "
                f"observed={row[key]!r}, expected={expected_value!r}."
            )
    if tuple(row["update_columns"]) != ("is_active",):
        raise RuntimeError(
            "Canonical deactivation trigger UPDATE OF columns drifted: "
            f"{tuple(row['update_columns'])!r}."
        )
    if not row["has_when_predicate"]:
        raise RuntimeError(
            "Canonical deactivation trigger WHEN predicate is absent."
        )

    trigger_definition = _normalized_sql(
        row["trigger_definition"] or ""
    ).lower().rstrip(";")

    trigger_definition_match = re.fullmatch(
        r".* for each row when \((.+)\) execute function "
        r"app_private\.handle_user_deactivation_cascade\(\)",
        trigger_definition,
    )

    if trigger_definition_match is None:
        raise RuntimeError(
            "Canonical deactivation trigger definition drifted: "
            f"{row['trigger_definition']!r}."
        )

    predicate = _strip_outer_parentheses(
        trigger_definition_match.group(1)
    )
    if predicate != "new.is_active=false":
        raise RuntimeError(
            "Canonical deactivation trigger predicate drifted: "
            f"{trigger_definition_match.group(1)!r}."
        )


def _require_duplicate_pair_absent(bind) -> None:
    routine_rows = bind.execute(
        sa.text(
            """
            SELECT
                routine.oid::bigint AS routine_oid,
                routine.prokind::text AS routine_kind,
                pg_catalog.pg_get_function_identity_arguments(
                    routine.oid
                )::text AS identity_arguments
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = :schema_name
              AND routine.proname = :duplicate_function
            ORDER BY routine.oid
            """
        ),
        {
            "schema_name": _PRIVATE_SCHEMA,
            "duplicate_function": _DUPLICATE_FUNCTION,
        },
    ).mappings().all()
    if routine_rows:
        raise RuntimeError(
            "Forbidden duplicate deactivation routine exists: "
            f"{[dict(row) for row in routine_rows]!r}."
        )

    trigger_rows = bind.execute(
        sa.text(
            """
            SELECT
                trigger.tgname::text AS trigger_name,
                relation_namespace.nspname::text AS relation_schema,
                relation.relname::text AS relation_name,
                function_namespace.nspname::text AS function_schema,
                routine.proname::text AS function_name
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_proc AS routine
              ON routine.oid = trigger.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = routine.pronamespace
            WHERE NOT trigger.tgisinternal
              AND (
                    trigger.tgname = :duplicate_trigger
                    OR (
                        function_namespace.nspname = :schema_name
                        AND routine.proname = :duplicate_function
                    )
              )
            ORDER BY relation_namespace.nspname, relation.relname,
                     trigger.tgname
            """
        ),
        {
            "duplicate_trigger": _DUPLICATE_TRIGGER,
            "schema_name": _PRIVATE_SCHEMA,
            "duplicate_function": _DUPLICATE_FUNCTION,
        },
    ).mappings().all()
    if trigger_rows:
        raise RuntimeError(
            "Forbidden duplicate deactivation trigger path exists: "
            f"{[dict(row) for row in trigger_rows]!r}."
        )


def _validate_canonical_deactivation_contract(bind) -> None:
    # DAFD_VALIDATION_ONLY_CONTRACT_START
    _require_execution_identity(bind)
    _require_role_contract(bind)
    _require_private_schema_contract(bind)
    _require_relation_columns(
        bind,
        "organization_users",
        {"id": "uuid", "is_active": "boolean", "org_id": "uuid"},
    )
    _require_relation_columns(
        bind,
        "organization_members",
        {
            "deleted_at": "timestamp with time zone",
            "id": "uuid",
            "org_id": "uuid",
            "user_id": "uuid",
        },
    )
    _require_relation_columns(
        bind,
        "branch_staff_roles",
        {
            "deleted_at": "timestamp with time zone",
            "org_id": "uuid",
            "organization_member_id": "uuid",
            "revoked_at": "timestamp with time zone",
            "revoked_by": "uuid",
        },
    )
    function_oid = _require_canonical_function(bind)
    _require_canonical_trigger(bind, function_oid)
    _require_duplicate_pair_absent(bind)
    # DAFD_VALIDATION_ONLY_CONTRACT_END


def upgrade() -> None:
    """Validate the canonical predecessor without adding duplicate behavior."""
    _validate_canonical_deactivation_contract(op.get_bind())


def downgrade() -> None:
    """Validate the unchanged predecessor without destroying its objects."""
    _validate_canonical_deactivation_contract(op.get_bind())
