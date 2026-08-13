"""RBAC Hardening Phase 1 — DB Roles, Extensions, Schemas, Privilege Bootstrap

Phase 1 of the v18.0 hardening plan.

Creates:
  • Extension: btree_gist (pinned); PostgreSQL 16 core SHA-2 and UUID functions require no pgcrypto lifecycle
  • DB roles: app_security_owner, app_runtime, audit_writer, readonly_analytics
    (app_migrator is the Alembic runner; not created here, assumed to exist)
  • Schema: app_secure  (security-barrier views)
  • Privilege revocations & grants on existing schemas

NOTE: This migration does NOT touch any application tables.
      It is purely infrastructure/governance.

Revision ID: 0022_rbac_phase1_roles_extensions
Revises: 0021_staff_roles
Create Date: 2026-05-23
"""

from alembic import op
import sqlalchemy as sa
import hashlib
import json

revision = "0022_rbac_p1_roles"
down_revision = "0021_staff_roles"
branch_labels = None
depends_on = None


# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START
from types import MappingProxyType

_RB1L7_MARKER_VERSION = 1
_RB1L7_REVISION = '0022_rbac_p1_roles'
_RB1L7_ACL_MARKER = 'app_private.migration_0022_schema_acl_state'
_RB1L7_ACL_OPERATIONS = (('REVOKE', 'app_private', 'PUBLIC', 'CREATE'), ('REVOKE', 'app_private', 'PUBLIC', 'USAGE'), ('GRANT', 'app_private', 'app_security_owner', 'USAGE'), ('REVOKE', 'public', 'PUBLIC', 'CREATE'), ('GRANT', 'public', 'app_runtime', 'USAGE'), ('GRANT', 'public', 'audit_writer', 'USAGE'), ('GRANT', 'public', 'readonly_analytics', 'USAGE'))


def _rb1l7_bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError(
            f"{_RB1L7_REVISION} requires online catalog access; "
            "offline Alembic SQL generation is unsupported."
        )
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable.")
    return bind


def _rb1l7_quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def _rb1l7_fetch_one(bind, sql, parameters=None):
    row = bind.execute(
        sa.text(sql),
        parameters or {},
    ).mappings().first()
    return dict(row) if row is not None else None


def _rb1l7_fetch_all(bind, sql, parameters=None):
    return [
        dict(row)
        for row in bind.execute(
            sa.text(sql),
            parameters or {},
        ).mappings().all()
    ]


def _rb1l7_identity(bind):
    row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            session_user::text AS session_user_name,
            current_user::text AS current_user_name,
            (SELECT oid::bigint FROM pg_catalog.pg_roles
             WHERE rolname = session_user) AS session_user_oid,
            (SELECT oid::bigint FROM pg_catalog.pg_roles
             WHERE rolname = current_user) AS current_user_oid
        """,
    )
    if (
        row is None
        or row["session_user_oid"] is None
        or row["current_user_oid"] is None
    ):
        raise RuntimeError("Could not resolve migration role identity.")
    return row


def _rb1l7_require_migration_owner(bind):
    identity = _rb1l7_identity(bind)
    if (
        identity["session_user_name"] != "migration_owner"
        or identity["current_user_name"] != "migration_owner"
        or identity["session_user_oid"] != identity["current_user_oid"]
    ):
        raise RuntimeError(
            "Shared-infrastructure migrations require both "
            "session_user and current_user to be migration_owner."
        )
    return identity



def _rb1l7_direct_acl_rows(
    bind,
    schema_name,
    grantee_name,
    privilege_type,
):
    rows = _rb1l7_fetch_all(
        bind,
        """
        SELECT
            namespace_data.oid::bigint AS schema_oid,
            namespace_data.nspname::text AS schema_name,
            namespace_data.nspowner::bigint AS schema_owner_oid,
            owner_role.rolname::text AS schema_owner_name,
            acl_data.grantor::bigint AS grantor_oid,
            grantor_role.rolname::text AS grantor_name,
            acl_data.grantee::bigint AS grantee_oid,
            CASE
                WHEN acl_data.grantee = 0 THEN 'PUBLIC'
                ELSE grantee_role.rolname::text
            END AS grantee_name,
            acl_data.privilege_type::text AS privilege_type,
            acl_data.is_grantable AS is_grantable
        FROM pg_catalog.pg_namespace AS namespace_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace_data.nspowner
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            namespace_data.nspacl
        ) AS acl_data
        LEFT JOIN pg_catalog.pg_roles AS grantor_role
          ON grantor_role.oid = acl_data.grantor
        LEFT JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl_data.grantee
        WHERE namespace_data.nspname = :schema_name
          AND acl_data.privilege_type = :privilege_type
          AND (
              (:grantee_name = 'PUBLIC' AND acl_data.grantee = 0)
              OR grantee_role.rolname = :grantee_name
          )
        ORDER BY
            acl_data.grantor,
            acl_data.grantee,
            acl_data.privilege_type,
            acl_data.is_grantable
        """,
        {
            "schema_name": schema_name,
            "grantee_name": grantee_name,
            "privilege_type": privilege_type,
        },
    )
    for row in rows:
        if row["grantor_name"] is None:
            raise RuntimeError(
                "Direct schema ACL row has an unresolved grantor."
            )
    return rows


def _rb1l7_acl_tuple(row):
    return (
        int(row["schema_oid"]),
        int(row["schema_owner_oid"]),
        int(row["grantor_oid"]),
        int(row["grantee_oid"]),
        str(row["privilege_type"]),
        bool(row["is_grantable"]),
    )


def _rb1l7_acl_fingerprint(rows):
    canonical = sorted(_rb1l7_acl_tuple(row) for row in rows)
    return hashlib.sha256(
        json.dumps(
            canonical,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rb1l7_capability(
    bind,
    role_name,
    schema_name,
    privilege_type,
):
    row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            namespace_data.nspowner = role_data.oid AS is_owner,
            pg_catalog.has_schema_privilege(
                role_data.rolname,
                namespace_data.nspname,
                :grant_option
            ) AS has_grant_option
        FROM pg_catalog.pg_namespace AS namespace_data
        JOIN pg_catalog.pg_roles AS role_data
          ON role_data.rolname = :role_name
        WHERE namespace_data.nspname = :schema_name
        """,
        {
            "role_name": role_name,
            "schema_name": schema_name,
            "grant_option": (
                privilege_type + " WITH GRANT OPTION"
            ),
        },
    )
    return bool(
        row
        and (
            row["is_owner"]
            or row["has_grant_option"]
        )
    )


def _rb1l7_restoration_context(bind, acl_row):
    _rb1l7_require_migration_owner(bind)
    identity = _rb1l7_identity(bind)
    grantor_name = str(acl_row["grantor_name"])

    if not _rb1l7_capability(
        bind,
        grantor_name,
        str(acl_row["schema_name"]),
        str(acl_row["privilege_type"]),
    ):
        raise RuntimeError(
            "Original grantor lacks current owner/grant-option "
            "capability required for exact restoration."
        )

    if grantor_name == identity["current_user_name"]:
        return {
            "restoration_role_oid": int(
                identity["current_user_oid"]
            ),
            "restoration_role_name": grantor_name,
            "restoration_mode": "CURRENT_ROLE",
            "set_role_required": False,
            "set_role_preflight_passed": True,
            "grant_option_preflight_passed": True,
        }

    set_result = _rb1l7_fetch_one(
        bind,
        """
        SELECT pg_catalog.pg_has_role(
            session_user,
            :grantor_name,
            'SET'
        ) AS can_set_role
        """,
        {"grantor_name": grantor_name},
    )
    if not set_result or not set_result["can_set_role"]:
        raise RuntimeError(
            "Exact grantor restoration is impossible: session_user "
            f"cannot SET ROLE to {grantor_name}."
        )

    return {
        "restoration_role_oid": int(acl_row["grantor_oid"]),
        "restoration_role_name": grantor_name,
        "restoration_mode": "SET_LOCAL_ROLE_ORIGINAL_GRANTOR",
        "set_role_required": True,
        "set_role_preflight_passed": True,
        "grant_option_preflight_passed": True,
    }


def _rb1l7_run_as(bind, role_name, sql):
    identity = _rb1l7_require_migration_owner(bind)
    if role_name == identity["current_user_name"]:
        bind.execute(sa.text(sql))
        return

    bind.execute(
        sa.text(
            "SET LOCAL ROLE "
            + _rb1l7_quote_ident(role_name)
        )
    )
    try:
        bind.execute(sa.text(sql))
    finally:
        bind.execute(sa.text("RESET ROLE"))
        _rb1l7_require_migration_owner(bind)



def _rb1l7_assert_relation_isolated(bind, qualified_name):
    _rb1l7_require_migration_owner(bind)
    relation = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            relation_data.oid::bigint AS relation_oid,
            relation_data.relkind::text AS relkind,
            relation_data.relowner::bigint AS owner_oid,
            owner_role.rolname::text AS owner_name
        FROM pg_catalog.pg_class AS relation_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = relation_data.relowner
        WHERE relation_data.oid = pg_catalog.to_regclass(:qualified_name)
        """,
        {"qualified_name": qualified_name},
    )
    if relation is None:
        raise RuntimeError(
            f"Marker relation {qualified_name} is absent."
        )
    if relation["owner_name"] != "migration_owner":
        raise RuntimeError(
            f"Marker relation {qualified_name} is not owned by "
            "migration_owner."
        )

    unexpected = _rb1l7_fetch_all(
        bind,
        """
        SELECT
            acl_data.grantor::bigint AS grantor_oid,
            grantor_role.rolname::text AS grantor_name,
            acl_data.grantee::bigint AS grantee_oid,
            CASE
                WHEN acl_data.grantee = 0 THEN 'PUBLIC'
                ELSE grantee_role.rolname::text
            END AS grantee_name,
            acl_data.privilege_type::text AS privilege_type,
            acl_data.is_grantable AS is_grantable
        FROM pg_catalog.pg_class AS relation_data
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            relation_data.relacl
        ) AS acl_data
        LEFT JOIN pg_catalog.pg_roles AS grantor_role
          ON grantor_role.oid = acl_data.grantor
        LEFT JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl_data.grantee
        WHERE relation_data.oid = pg_catalog.to_regclass(:qualified_name)
          AND (
              acl_data.grantee = 0
              OR acl_data.grantee <> relation_data.relowner
          )
        ORDER BY
            acl_data.grantee,
            acl_data.grantor,
            acl_data.privilege_type,
            acl_data.is_grantable
        """,
        {"qualified_name": qualified_name},
    )
    if unexpected:
        raise RuntimeError(
            f"Marker relation {qualified_name} has direct privileges "
            "for PUBLIC or a non-owner role; default privileges must "
            "be corrected outside this migration."
        )


def _rb1l7_assert_marker_isolated(
    bind,
    marker_name,
    identity_column=None,
):
    _rb1l7_assert_relation_isolated(bind, marker_name)
    if identity_column is None:
        return

    sequence = _rb1l7_fetch_one(
        bind,
        """
        SELECT pg_catalog.pg_get_serial_sequence(
            :marker_name,
            :identity_column
        ) AS sequence_name
        """,
        {
            "marker_name": marker_name,
            "identity_column": identity_column,
        },
    )
    if sequence is None or sequence["sequence_name"] is None:
        raise RuntimeError(
            f"Identity sequence for {marker_name}.{identity_column} "
            "could not be resolved."
        )
    _rb1l7_assert_relation_isolated(
        bind,
        sequence["sequence_name"],
    )

def _rb1l7_marker_exists(bind):
    row = _rb1l7_fetch_one(
        bind,
        "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
        {"name": _RB1L7_ACL_MARKER},
    )
    return bool(row and row["present"])


def _rb1l7_create_acl_marker(bind):
    _rb1l7_require_migration_owner(bind)
    if _rb1l7_marker_exists(bind):
        raise RuntimeError(
            f"Marker collision: {_RB1L7_ACL_MARKER} already exists."
        )

    bind.execute(
        sa.text(
            """
            CREATE TABLE """
            + _RB1L7_ACL_MARKER
            + """ (
                state_id BIGINT GENERATED ALWAYS AS IDENTITY
                    PRIMARY KEY,
                marker_version SMALLINT NOT NULL,
                revision TEXT NOT NULL,
                expected_operation_count SMALLINT NOT NULL,
                operation_ordinal SMALLINT NOT NULL,
                schema_oid OID NOT NULL,
                schema_name NAME NOT NULL,
                schema_owner_oid OID NOT NULL,
                schema_owner_name TEXT NOT NULL,
                mutation_kind TEXT NOT NULL,
                grantee_oid OID,
                grantee_name TEXT NOT NULL,
                privilege_type TEXT NOT NULL,
                direct_row_existed BOOLEAN NOT NULL,
                original_grantor_oid OID,
                original_grantor_name TEXT,
                original_is_grantable BOOLEAN,
                restoration_role_oid OID,
                restoration_role_name TEXT,
                restoration_mode TEXT,
                set_role_required BOOLEAN NOT NULL,
                set_role_preflight_passed BOOLEAN NOT NULL,
                grant_option_preflight_passed BOOLEAN NOT NULL,
                mutation_applied BOOLEAN NOT NULL,
                added_by_revision BOOLEAN NOT NULL,
                removed_by_revision BOOLEAN NOT NULL,
                resulting_grantor_oid OID,
                resulting_grantor_name TEXT,
                resulting_is_grantable BOOLEAN,
                prestate_fingerprint TEXT NOT NULL,
                poststate_fingerprint TEXT,
                state_finalized BOOLEAN NOT NULL DEFAULT FALSE,
                state_digest TEXT,
                captured_at TIMESTAMPTZ NOT NULL
                    DEFAULT pg_catalog.clock_timestamp(),
                CHECK (
                    restoration_mode IS NULL
                    OR restoration_mode IN (
                        'CURRENT_ROLE',
                        'SET_LOCAL_ROLE_ORIGINAL_GRANTOR'
                    )
                )
            )
            """
        )
    )
    _rb1l7_assert_marker_isolated(
        bind,
        _RB1L7_ACL_MARKER,
        identity_column="state_id",
    )


def _rb1l7_insert_acl_marker(
    bind,
    *,
    ordinal,
    action,
    schema_name,
    grantee_name,
    privilege_type,
    before_rows,
    original_row,
    restoration,
    mutation_applied,
    added_by_revision,
    removed_by_revision,
    resulting_row,
    post_rows,
):
    schema_row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            namespace_data.oid::bigint AS schema_oid,
            namespace_data.nspowner::bigint AS schema_owner_oid,
            owner_role.rolname::text AS schema_owner_name,
            CASE
                WHEN :grantee_name = 'PUBLIC' THEN 0::bigint
                ELSE grantee_role.oid::bigint
            END AS grantee_oid
        FROM pg_catalog.pg_namespace AS namespace_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace_data.nspowner
        LEFT JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.rolname = NULLIF(:grantee_name, 'PUBLIC')
        WHERE namespace_data.nspname = :schema_name
        """,
        {
            "schema_name": schema_name,
            "grantee_name": grantee_name,
        },
    )
    if schema_row is None:
        raise RuntimeError(f"Schema {schema_name} is absent.")

    bind.execute(
        sa.text(
            """
            INSERT INTO """
            + _RB1L7_ACL_MARKER
            + """ (
                marker_version,
                revision,
                expected_operation_count,
                operation_ordinal,
                schema_oid,
                schema_name,
                schema_owner_oid,
                schema_owner_name,
                mutation_kind,
                grantee_oid,
                grantee_name,
                privilege_type,
                direct_row_existed,
                original_grantor_oid,
                original_grantor_name,
                original_is_grantable,
                restoration_role_oid,
                restoration_role_name,
                restoration_mode,
                set_role_required,
                set_role_preflight_passed,
                grant_option_preflight_passed,
                mutation_applied,
                added_by_revision,
                removed_by_revision,
                resulting_grantor_oid,
                resulting_grantor_name,
                resulting_is_grantable,
                prestate_fingerprint,
                poststate_fingerprint
            ) VALUES (
                :marker_version,
                :revision,
                :expected_operation_count,
                :operation_ordinal,
                :schema_oid,
                :schema_name,
                :schema_owner_oid,
                :schema_owner_name,
                :mutation_kind,
                :grantee_oid,
                :grantee_name,
                :privilege_type,
                :direct_row_existed,
                :original_grantor_oid,
                :original_grantor_name,
                :original_is_grantable,
                :restoration_role_oid,
                :restoration_role_name,
                :restoration_mode,
                :set_role_required,
                :set_role_preflight_passed,
                :grant_option_preflight_passed,
                :mutation_applied,
                :added_by_revision,
                :removed_by_revision,
                :resulting_grantor_oid,
                :resulting_grantor_name,
                :resulting_is_grantable,
                :prestate_fingerprint,
                :poststate_fingerprint
            )
            """
        ),
        {
            "marker_version": _RB1L7_MARKER_VERSION,
            "revision": _RB1L7_REVISION,
            "expected_operation_count": len(
                _RB1L7_ACL_OPERATIONS
            ),
            "operation_ordinal": ordinal,
            "schema_oid": schema_row["schema_oid"],
            "schema_name": schema_name,
            "schema_owner_oid": schema_row["schema_owner_oid"],
            "schema_owner_name": schema_row["schema_owner_name"],
            "mutation_kind": action,
            "grantee_oid": schema_row["grantee_oid"],
            "grantee_name": grantee_name,
            "privilege_type": privilege_type,
            "direct_row_existed": original_row is not None,
            "original_grantor_oid": (
                original_row["grantor_oid"]
                if original_row
                else None
            ),
            "original_grantor_name": (
                original_row["grantor_name"]
                if original_row
                else None
            ),
            "original_is_grantable": (
                original_row["is_grantable"]
                if original_row
                else None
            ),
            "restoration_role_oid": (
                restoration["restoration_role_oid"]
                if restoration
                else None
            ),
            "restoration_role_name": (
                restoration["restoration_role_name"]
                if restoration
                else None
            ),
            "restoration_mode": (
                restoration["restoration_mode"]
                if restoration
                else None
            ),
            "set_role_required": bool(
                restoration
                and restoration["set_role_required"]
            ),
            "set_role_preflight_passed": bool(
                restoration
                and restoration[
                    "set_role_preflight_passed"
                ]
            ),
            "grant_option_preflight_passed": bool(
                restoration
                and restoration[
                    "grant_option_preflight_passed"
                ]
            ),
            "mutation_applied": mutation_applied,
            "added_by_revision": added_by_revision,
            "removed_by_revision": removed_by_revision,
            "resulting_grantor_oid": (
                resulting_row["grantor_oid"]
                if resulting_row
                else None
            ),
            "resulting_grantor_name": (
                resulting_row["grantor_name"]
                if resulting_row
                else None
            ),
            "resulting_is_grantable": (
                resulting_row["is_grantable"]
                if resulting_row
                else None
            ),
            "prestate_fingerprint": _rb1l7_acl_fingerprint(
                before_rows
            ),
            "poststate_fingerprint": _rb1l7_acl_fingerprint(
                post_rows
            ),
        },
    )


def _rb1l7_grant_sql(
    schema_name,
    grantee_name,
    privilege_type,
    grantable,
):
    sql = (
        "GRANT "
        + privilege_type
        + " ON SCHEMA "
        + _rb1l7_quote_ident(schema_name)
        + " TO "
        + (
            "PUBLIC"
            if grantee_name == "PUBLIC"
            else _rb1l7_quote_ident(grantee_name)
        )
    )
    if grantable:
        sql += " WITH GRANT OPTION"
    return sql


def _rb1l7_revoke_sql(
    schema_name,
    grantee_name,
    privilege_type,
):
    return (
        "REVOKE "
        + privilege_type
        + " ON SCHEMA "
        + _rb1l7_quote_ident(schema_name)
        + " FROM "
        + (
            "PUBLIC"
            if grantee_name == "PUBLIC"
            else _rb1l7_quote_ident(grantee_name)
        )
    )


def _rb1l7_apply_acl_operations(bind):
    _rb1l7_require_migration_owner(bind)
    for ordinal, operation in enumerate(
        _RB1L7_ACL_OPERATIONS,
        start=1,
    ):
        (
            action,
            schema_name,
            grantee_name,
            privilege_type,
        ) = operation
        before_rows = _rb1l7_direct_acl_rows(
            bind,
            schema_name,
            grantee_name,
            privilege_type,
        )

        if action == "GRANT":
            if before_rows:
                for original_row in before_rows:
                    _rb1l7_insert_acl_marker(
                        bind,
                        ordinal=ordinal,
                        action=action,
                        schema_name=schema_name,
                        grantee_name=grantee_name,
                        privilege_type=privilege_type,
                        before_rows=before_rows,
                        original_row=original_row,
                        restoration=None,
                        mutation_applied=False,
                        added_by_revision=False,
                        removed_by_revision=False,
                        resulting_row=original_row,
                        post_rows=before_rows,
                    )
                continue

            bind.execute(
                sa.text(
                    _rb1l7_grant_sql(
                        schema_name,
                        grantee_name,
                        privilege_type,
                        False,
                    )
                )
            )
            after_rows = _rb1l7_direct_acl_rows(
                bind,
                schema_name,
                grantee_name,
                privilege_type,
            )
            delta = [
                row
                for row in after_rows
                if _rb1l7_acl_tuple(row)
                not in {
                    _rb1l7_acl_tuple(item)
                    for item in before_rows
                }
            ]
            if len(delta) != 1:
                raise RuntimeError(
                    "A direct schema GRANT did not produce exactly "
                    "one deterministic ACL row."
                )
            resulting_row = delta[0]
            restoration = _rb1l7_restoration_context(
                bind,
                resulting_row,
            )
            _rb1l7_insert_acl_marker(
                bind,
                ordinal=ordinal,
                action=action,
                schema_name=schema_name,
                grantee_name=grantee_name,
                privilege_type=privilege_type,
                before_rows=before_rows,
                original_row=None,
                restoration=restoration,
                mutation_applied=True,
                added_by_revision=True,
                removed_by_revision=False,
                resulting_row=resulting_row,
                post_rows=after_rows,
            )
            continue

        if action != "REVOKE":
            raise RuntimeError(f"Unsupported ACL action: {action}")

        if not before_rows:
            _rb1l7_insert_acl_marker(
                bind,
                ordinal=ordinal,
                action=action,
                schema_name=schema_name,
                grantee_name=grantee_name,
                privilege_type=privilege_type,
                before_rows=before_rows,
                original_row=None,
                restoration=None,
                mutation_applied=False,
                added_by_revision=False,
                removed_by_revision=False,
                resulting_row=None,
                post_rows=before_rows,
            )
            continue

        prepared = [
            (
                row,
                _rb1l7_restoration_context(bind, row),
            )
            for row in before_rows
        ]

        for original_row, restoration in prepared:
            _rb1l7_run_as(
                bind,
                restoration["restoration_role_name"],
                _rb1l7_revoke_sql(
                    schema_name,
                    grantee_name,
                    privilege_type,
                ),
            )
            after_rows = _rb1l7_direct_acl_rows(
                bind,
                schema_name,
                grantee_name,
                privilege_type,
            )
            if _rb1l7_acl_tuple(original_row) in {
                _rb1l7_acl_tuple(item)
                for item in after_rows
            }:
                raise RuntimeError(
                    "Targeted direct ACL row survived REVOKE."
                )
            _rb1l7_insert_acl_marker(
                bind,
                ordinal=ordinal,
                action=action,
                schema_name=schema_name,
                grantee_name=grantee_name,
                privilege_type=privilege_type,
                before_rows=before_rows,
                original_row=original_row,
                restoration=restoration,
                mutation_applied=True,
                added_by_revision=False,
                removed_by_revision=True,
                resulting_row=None,
                post_rows=after_rows,
            )


def _rb1l7_marker_payload(bind):
    return _rb1l7_fetch_all(
        bind,
        """
        SELECT *
        FROM """
        + _RB1L7_ACL_MARKER
        + """
        ORDER BY
            operation_ordinal,
            state_id
        """,
    )


def _rb1l7_marker_digest(rows):
    normalized = []
    for row in rows:
        item = dict(row)
        item.pop("state_digest", None)
        item.pop("state_finalized", None)
        item.pop("captured_at", None)
        normalized.append(item)
    return hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rb1l7_finalize_acl_marker(bind):
    _rb1l7_require_migration_owner(bind)
    rows = _rb1l7_marker_payload(bind)
    ordinals = {
        int(row["operation_ordinal"])
        for row in rows
    }
    expected = set(
        range(1, len(_RB1L7_ACL_OPERATIONS) + 1)
    )
    if ordinals != expected:
        raise RuntimeError(
            "ACL marker operation ordinals are incomplete."
        )
    digest = _rb1l7_marker_digest(rows)
    bind.execute(
        sa.text(
            "UPDATE "
            + _RB1L7_ACL_MARKER
            + """
              SET state_finalized = TRUE,
                  state_digest = :digest
            """
        ),
        {"digest": digest},
    )


def _rb1l7_load_acl_marker(bind):
    _rb1l7_require_migration_owner(bind)
    if not _rb1l7_marker_exists(bind):
        raise RuntimeError(
            f"Required marker {_RB1L7_ACL_MARKER} is absent."
        )
    rows = _rb1l7_marker_payload(bind)
    if not rows:
        raise RuntimeError("ACL marker is empty.")
    if any(
        int(row["marker_version"]) != _RB1L7_MARKER_VERSION
        or row["revision"] != _RB1L7_REVISION
        or int(row["expected_operation_count"])
        != len(_RB1L7_ACL_OPERATIONS)
        or not row["state_finalized"]
        or not row["state_digest"]
        for row in rows
    ):
        raise RuntimeError(
            "ACL marker version/count/finalized state is invalid."
        )
    ordinals = {
        int(row["operation_ordinal"])
        for row in rows
    }
    expected = set(
        range(1, len(_RB1L7_ACL_OPERATIONS) + 1)
    )
    if ordinals != expected:
        raise RuntimeError(
            "ACL marker operation ordinals are invalid."
        )
    digests = {row["state_digest"] for row in rows}
    if len(digests) != 1:
        raise RuntimeError("ACL marker digest is inconsistent.")
    if _rb1l7_marker_digest(rows) != next(iter(digests)):
        raise RuntimeError("ACL marker digest verification failed.")
    return tuple(
        MappingProxyType(dict(row))
        for row in rows
    )


def _rb1l7_restore_acl_rows(bind, rows):
    _rb1l7_require_migration_owner(bind)
    for row in rows:
        if row["added_by_revision"]:
            role_name = row["resulting_grantor_name"]
            if not role_name:
                raise RuntimeError(
                    "Added ACL row lacks resulting grantor."
                )
            _rb1l7_run_as(
                bind,
                role_name,
                _rb1l7_revoke_sql(
                    row["schema_name"],
                    row["grantee_name"],
                    row["privilege_type"],
                ),
            )
            remaining = _rb1l7_direct_acl_rows(
                bind,
                row["schema_name"],
                row["grantee_name"],
                row["privilege_type"],
            )
            target = (
                int(row["schema_oid"]),
                int(row["schema_owner_oid"]),
                int(row["resulting_grantor_oid"]),
                int(row["grantee_oid"]),
                str(row["privilege_type"]),
                bool(row["resulting_is_grantable"]),
            )
            if target in {
                _rb1l7_acl_tuple(item)
                for item in remaining
            }:
                raise RuntimeError(
                    "Revision-added ACL row survived downgrade."
                )

        if row["removed_by_revision"]:
            role_name = row["restoration_role_name"]
            if not role_name:
                raise RuntimeError(
                    "Removed ACL row lacks restoration role."
                )
            _rb1l7_run_as(
                bind,
                role_name,
                _rb1l7_grant_sql(
                    row["schema_name"],
                    row["grantee_name"],
                    row["privilege_type"],
                    bool(row["original_is_grantable"]),
                ),
            )
            restored = _rb1l7_direct_acl_rows(
                bind,
                row["schema_name"],
                row["grantee_name"],
                row["privilege_type"],
            )
            target = (
                int(row["schema_oid"]),
                int(row["schema_owner_oid"]),
                int(row["original_grantor_oid"]),
                int(row["grantee_oid"]),
                str(row["privilege_type"]),
                bool(row["original_is_grantable"]),
            )
            if target not in {
                _rb1l7_acl_tuple(item)
                for item in restored
            }:
                raise RuntimeError(
                    "Exact original ACL grantor row was not restored."
                )


def _rb1l7_prepare_revision_schema_acl_state():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    _rb1l7_create_acl_marker(bind)
    _rb1l7_apply_acl_operations(bind)


def _rb1l7_finalize_revision_schema_acl_state():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    _rb1l7_finalize_acl_marker(bind)


def _rb1l7_restore_revision_schema_acl_state():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    rows = _rb1l7_load_acl_marker(bind)
    _rb1l7_restore_acl_rows(bind, rows)
    bind.execute(
        sa.text(
            "DROP TABLE "
            + _RB1L7_ACL_MARKER
            + " RESTRICT"
        )
    )
# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_END



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_managed_roles() -> str:
    """Return read-only validation for externally managed RBAC roles."""
    return r"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM (VALUES
                    ('app_security_owner'),
                    ('app_runtime'),
                    ('audit_writer'),
                    ('readonly_analytics')
                ) AS required(role_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles AS role_data
                    WHERE role_data.rolname = required.role_name
                )
            ) THEN
                RAISE EXCEPTION
                    'Required managed cluster roles are missing; security/cluster_role_bootstrap contract must be applied before Alembic migrations.';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS role_data
                WHERE role_data.rolname IN (
                    'app_security_owner',
                    'app_runtime',
                    'audit_writer',
                    'readonly_analytics'
                )
                  AND (
                        role_data.rolsuper
                     OR role_data.rolbypassrls
                     OR role_data.rolcanlogin
                     OR role_data.rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'Managed cluster role attributes violate the approved security/cluster_role_bootstrap contract.';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS role_data
                WHERE role_data.rolname = 'app_runtime'
                  AND (
                        NOT ('statement_timeout=5s' = ANY(COALESCE(role_data.rolconfig, ARRAY[]::text[])))
                     OR NOT ('lock_timeout=2s' = ANY(COALESCE(role_data.rolconfig, ARRAY[]::text[])))
                     OR NOT ('row_security=on' = ANY(COALESCE(role_data.rolconfig, ARRAY[]::text[])))
                  )
            ) THEN
                RAISE EXCEPTION
                    'app_runtime settings violate the approved security/cluster_role_bootstrap/role_settings.v1.json contract.';
            END IF;
        END
        $$;
    """

# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------



# RB1L8D1D2_APP_SECURE_OWNER_CONTEXT_HELPERS

def _rb1l8d1d2_preflight_app_secure_owner_context(
    bind,
    *,
    require_schema,
):
    """Validate app_secure owner context without mutating catalog state."""
    _rb1l7_require_migration_owner(bind)
    row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS role_data
                WHERE role_data.rolname = 'app_security_owner'
            ) AS target_role_exists,
            pg_catalog.pg_has_role(
                session_user,
                'app_security_owner',
                'SET'
            ) AS can_set_target_role,
            pg_catalog.has_database_privilege(
                current_user,
                current_database(),
                'CREATE'
            ) AS can_create_schema,
            namespace_data.oid IS NOT NULL AS schema_exists,
            pg_catalog.pg_get_userbyid(
                namespace_data.nspowner
            )::text AS schema_owner_name
        FROM (SELECT 1) AS singleton
        LEFT JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.nspname = 'app_secure'
        """,
    )
    if row is None:
        raise RuntimeError(
            "app_secure owner-context preflight returned no row."
        )
    if not row["target_role_exists"]:
        raise RuntimeError(
            "Required managed role app_security_owner is absent."
        )
    if not row["can_set_target_role"]:
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    if row["schema_exists"]:
        if row["schema_owner_name"] != "app_security_owner":
            raise RuntimeError(
                "app_secure must be owned by app_security_owner; "
                f"found {row['schema_owner_name']}."
            )
    else:
        if require_schema:
            raise RuntimeError(
                "app_secure is required for bounded owner-context "
                "downgrade operations."
            )
        if not row["can_create_schema"]:
            raise RuntimeError(
                "migration_owner lacks database CREATE required to "
                "create app_secure."
            )


def _rb1l8d1d2_assert_app_secure_owner(bind):
    """Require the current app_secure owner to be app_security_owner."""
    _rb1l7_require_migration_owner(bind)
    row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            pg_catalog.pg_get_userbyid(
                namespace_data.nspowner
            )::text AS schema_owner_name
        FROM pg_catalog.pg_namespace AS namespace_data
        WHERE namespace_data.nspname = 'app_secure'
        """,
    )
    if row is None:
        raise RuntimeError(
            "app_secure was not created by revision 0022."
        )
    if row["schema_owner_name"] != "app_security_owner":
        raise RuntimeError(
            "app_secure must be owned by app_security_owner; "
            f"found {row['schema_owner_name']}."
        )

def upgrade() -> None:

    # ── 1. Extensions ────────────────────────────────────────────────────
    # btree_gist: required for temporal EXCLUDE constraints on branch_staff_roles
    # PostgreSQL 16 core sha256() is used; pgcrypto is not migration-owned.
    bind = _rb1l7_bind()
    _rb1l8d1d2_preflight_app_secure_owner_context(bind, require_schema=False)

    # ── 2. Externally managed DB-role validation ────────────────────────
    op.execute(_validate_managed_roles())

    # ── 3. app_secure Schema ─────────────────────────────────────────────
    # Houses security-barrier views. Owned by app_security_owner.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'app_secure') THEN
                CREATE SCHEMA app_secure AUTHORIZATION app_security_owner;
            END IF;
        END$$;
    """)
    _rb1l8d1d2_assert_app_secure_owner(bind)

    # Revoke PUBLIC access; grant USAGE only to runtime roles.
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        "REVOKE ALL ON SCHEMA app_secure FROM PUBLIC;",
    )
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        "GRANT USAGE ON SCHEMA app_secure TO app_runtime, readonly_analytics;",
    )

    # Ensure future objects in app_secure are not exposed to PUBLIC by default.
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure
        REVOKE ALL ON TABLES FROM PUBLIC;
    """,
    )
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure
        GRANT SELECT ON TABLES TO app_runtime;
    """,
    )

    # ── 4. app_private Schema Hardening ──────────────────────────────────
    # app_private is conditionally owned by 0020_contacts_hardened.
    # Tighten PUBLIC access.
    _rb1l7_prepare_revision_schema_acl_state()
    _rb1l7_finalize_revision_schema_acl_state()

    # ── 5. public Schema Hardening ────────────────────────────────────────
    # Restrict PUBLIC from having blanket rights on public schema.

    # Grant schema-level access to runtime roles.

    # Role comments are externally managed by security/cluster_role_bootstrap.


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Schema comments
    bind = _rb1l7_bind()
    _rb1l8d1d2_preflight_app_secure_owner_context(bind, require_schema=True)
    _rb1l7_restore_revision_schema_acl_state()
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        "COMMENT ON SCHEMA app_secure IS NULL;",
    )

    # Restore public schema CREATE grant for PUBLIC (undo restriction)

    # Drop app_secure schema (must be empty first due to CASCADE behaviour)
    _rb1l7_run_as(
        bind,
        "app_security_owner",
        "DROP SCHEMA IF EXISTS app_secure CASCADE;",
    )

    # Externally managed cluster roles, attributes, settings, comments,
    # and memberships are preserved by downgrade.

    # Shared extensions are external prerequisites and are intentionally preserved.
