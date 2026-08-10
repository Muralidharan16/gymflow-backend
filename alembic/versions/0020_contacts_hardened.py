"""
Branch Contacts Final Hardened Implementation
Revision ID: 0020_contacts_hardened
Revises: 00f277c748ea
Create Date: 2026-05-22
Purpose: Zero-downtime safe deployment with elite hyperscale hardening

CRITICAL DEPLOYMENT NOTES:
=========================
1. Phase A (this file): Schema + NOT VALID constraints + online-safe indices
2. Phase B: Application deployment with app-layer validation
3. Phase C: Async constraint validation in maintenance window
4. Phase D: Remove app-layer checks, DB becomes enforcement source

CREATE INDEX CONCURRENTLY statements execute in autocommit blocks.
Partitioned audit-table indexes are created non-concurrently while tables are empty.
All SECURITY DEFINER functions use minimal search_path (pg_catalog only).
No PUBLIC EXECUTE grants on sensitive functions.
All advisory locks use native hashtextextended() instead of md5().
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid
import hashlib
import json

# revision identifiers, used by Alembic.
revision = "0020_contacts_hardened"
down_revision = "00f277c748ea"
branch_labels = None
depends_on = None


# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START
from types import MappingProxyType

_RB1L7_MARKER_VERSION = 1
_RB1L7_REVISION = '0020_contacts_hardened'
_RB1L7_ACL_MARKER = 'app_private.migration_0020_schema_acl_state'
_RB1L7_ACL_OPERATIONS = (('REVOKE', 'app_private', 'PUBLIC', 'CREATE'), ('REVOKE', 'app_private', 'PUBLIC', 'USAGE'), ('GRANT', 'app_private', 'app_rls_executor', 'USAGE'), ('GRANT', 'public', 'app_rls_executor', 'CREATE'), ('GRANT', 'public', 'app_rls_executor', 'USAGE'))


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

_RB1L7_HEADER_MARKER = (
    "app_private.migration_0020_shared_infrastructure_state"
)
_RB1L7_TEMP_OPERATION_ORDINAL = 6
_RB1L7_TEMP_CREATE_PRESTATE = None
_RB1L7_TEMP_CREATE_ADDED_ROW = None
_RB1L7_TEMP_CREATE_PREPARED = False
_RB1L7_TEMP_GRANT_SQL = (
    "GRANT CREATE ON SCHEMA app_private "
    "TO app_rls_executor;"
)
_RB1L7_TEMP_REVOKE_SQL = (
    "REVOKE CREATE ON SCHEMA app_private "
    "FROM app_rls_executor;"
)


def _rb1l7_normalize_sql(value):
    if not isinstance(value, str):
        return None
    return " ".join(value.strip().split()).upper()


class _RB1L7UpgradeOperations:
    """Delegate Alembic operations while conditionally executing temp ACL SQL."""

    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def execute(self, sql, *args, **kwargs):
        normalized = _rb1l7_normalize_sql(sql)
        if normalized == _rb1l7_normalize_sql(
            _RB1L7_TEMP_GRANT_SQL
        ):
            return _rb1l7_execute_temporary_create_grant(
                self._delegate,
                sql,
                args,
                kwargs,
            )
        if normalized == _rb1l7_normalize_sql(
            _RB1L7_TEMP_REVOKE_SQL
        ):
            return _rb1l7_execute_temporary_create_revoke(
                self._delegate,
                sql,
                args,
                kwargs,
            )
        return self._delegate.execute(sql, *args, **kwargs)


def _rb1l7_upgrade_operations(delegate):
    return _RB1L7UpgradeOperations(delegate)


def _rb1l7_validate_0020_roles(bind):
    rows = _rb1l7_fetch_all(
        bind,
        """
        SELECT
            role_data.rolname,
            role_data.rolsuper,
            role_data.rolbypassrls,
            role_data.rolcanlogin
        FROM pg_catalog.pg_roles AS role_data
        WHERE role_data.rolname IN (
            'migration_owner',
            'app_rls_executor',
            'app_user'
        )
        ORDER BY role_data.rolname
        """,
    )
    by_name = {
        row["rolname"]: row
        for row in rows
    }
    if set(by_name) != {
        "migration_owner",
        "app_rls_executor",
        "app_user",
    }:
        raise RuntimeError(
            "Required externally managed roles are absent."
        )
    for role_name in ("app_rls_executor", "app_user"):
        row = by_name[role_name]
        if (
            row["rolsuper"]
            or row["rolbypassrls"]
            or row["rolcanlogin"]
        ):
            raise RuntimeError(
                f"Managed role {role_name} violates the "
                "approved bootstrap contract."
            )


def _rb1l7_schema_state(bind, schema_name):
    return _rb1l7_fetch_one(
        bind,
        """
        SELECT
            current_database()::text AS database_name,
            (
                SELECT oid::bigint
                FROM pg_catalog.pg_database
                WHERE datname = current_database()
            ) AS database_oid,
            namespace_data.oid::bigint AS schema_oid,
            namespace_data.nspname::text AS schema_name,
            namespace_data.nspowner::bigint AS owner_oid,
            owner_role.rolname::text AS owner_name,
            namespace_data.nspacl IS NULL AS nspacl_was_null,
            pg_catalog.has_schema_privilege(
                current_user,
                namespace_data.nspname,
                'USAGE'
            ) AS current_role_has_usage,
            pg_catalog.has_schema_privilege(
                current_user,
                namespace_data.nspname,
                'CREATE'
            ) AS current_role_has_create
        FROM pg_catalog.pg_namespace AS namespace_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace_data.nspowner
        WHERE namespace_data.nspname = :schema_name
        """,
        {"schema_name": schema_name},
    )


def _rb1l7_all_direct_acl_rows(bind, schema_name):
    return _rb1l7_fetch_all(
        bind,
        """
        SELECT
            namespace_data.oid::bigint AS schema_oid,
            namespace_data.nspowner::bigint AS schema_owner_oid,
            acl_data.grantor::bigint AS grantor_oid,
            acl_data.grantee::bigint AS grantee_oid,
            acl_data.privilege_type::text AS privilege_type,
            acl_data.is_grantable AS is_grantable
        FROM pg_catalog.pg_namespace AS namespace_data
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            namespace_data.nspacl
        ) AS acl_data
        WHERE namespace_data.nspname = :schema_name
        ORDER BY
            acl_data.grantor,
            acl_data.grantee,
            acl_data.privilege_type,
            acl_data.is_grantable
        """,
        {"schema_name": schema_name},
    )


def _rb1l7_header_exists(bind):
    row = _rb1l7_fetch_one(
        bind,
        "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
        {"name": _RB1L7_HEADER_MARKER},
    )
    return bool(row and row["present"])


def _rb1l7_create_header_marker(
    bind,
    *,
    existed_before,
    original_state,
    prepared_state,
    original_acl_fingerprint,
    identity,
):
    _rb1l7_require_migration_owner(bind)
    if _rb1l7_header_exists(bind):
        raise RuntimeError(
            f"Marker collision: {_RB1L7_HEADER_MARKER} already exists."
        )

    bind.execute(
        sa.text(
            """
            CREATE TABLE """
            + _RB1L7_HEADER_MARKER
            + """ (
                singleton BOOLEAN PRIMARY KEY CHECK (singleton),
                marker_version SMALLINT NOT NULL,
                revision TEXT NOT NULL,
                database_oid OID NOT NULL,
                database_name TEXT NOT NULL,
                migration_session_user_oid OID NOT NULL,
                migration_session_user_name TEXT NOT NULL,
                migration_current_user_oid OID NOT NULL,
                migration_current_user_name TEXT NOT NULL,
                app_private_existed_before BOOLEAN NOT NULL,
                app_private_created_by_revision BOOLEAN NOT NULL,
                app_private_oid_after_prepare OID NOT NULL,
                original_owner_oid OID,
                original_owner_name TEXT,
                owner_after_prepare_oid OID NOT NULL,
                owner_after_prepare_name TEXT NOT NULL,
                original_nspacl_was_null BOOLEAN NOT NULL,
                original_direct_acl_fingerprint TEXT NOT NULL,
                migration_role_had_usage_before BOOLEAN NOT NULL,
                migration_role_had_create_before BOOLEAN NOT NULL,
                schema_compatibility_preflight_passed BOOLEAN NOT NULL,
                marker_collision_preflight_passed BOOLEAN NOT NULL,
                expected_acl_operation_count SMALLINT NOT NULL,
                state_finalized BOOLEAN NOT NULL DEFAULT FALSE,
                state_digest TEXT,
                captured_at TIMESTAMPTZ NOT NULL
                    DEFAULT pg_catalog.clock_timestamp()
            )
            """
        )
    )
    _rb1l7_assert_marker_isolated(
        bind,
        _RB1L7_HEADER_MARKER,
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO """
            + _RB1L7_HEADER_MARKER
            + """ (
                singleton,
                marker_version,
                revision,
                database_oid,
                database_name,
                migration_session_user_oid,
                migration_session_user_name,
                migration_current_user_oid,
                migration_current_user_name,
                app_private_existed_before,
                app_private_created_by_revision,
                app_private_oid_after_prepare,
                original_owner_oid,
                original_owner_name,
                owner_after_prepare_oid,
                owner_after_prepare_name,
                original_nspacl_was_null,
                original_direct_acl_fingerprint,
                migration_role_had_usage_before,
                migration_role_had_create_before,
                schema_compatibility_preflight_passed,
                marker_collision_preflight_passed,
                expected_acl_operation_count
            ) VALUES (
                TRUE,
                :marker_version,
                :revision,
                :database_oid,
                :database_name,
                :session_user_oid,
                :session_user_name,
                :current_user_oid,
                :current_user_name,
                :existed_before,
                :created_by_revision,
                :prepared_oid,
                :original_owner_oid,
                :original_owner_name,
                :prepared_owner_oid,
                :prepared_owner_name,
                :original_nspacl_was_null,
                :original_acl_fingerprint,
                :had_usage_before,
                :had_create_before,
                TRUE,
                TRUE,
                :expected_acl_operation_count
            )
            """
        ),
        {
            "marker_version": _RB1L7_MARKER_VERSION,
            "revision": _RB1L7_REVISION,
            "database_oid": prepared_state["database_oid"],
            "database_name": prepared_state["database_name"],
            "session_user_oid": identity["session_user_oid"],
            "session_user_name": identity["session_user_name"],
            "current_user_oid": identity["current_user_oid"],
            "current_user_name": identity["current_user_name"],
            "existed_before": existed_before,
            "created_by_revision": not existed_before,
            "prepared_oid": prepared_state["schema_oid"],
            "original_owner_oid": (
                original_state["owner_oid"]
                if original_state
                else None
            ),
            "original_owner_name": (
                original_state["owner_name"]
                if original_state
                else None
            ),
            "prepared_owner_oid": prepared_state["owner_oid"],
            "prepared_owner_name": prepared_state["owner_name"],
            "original_nspacl_was_null": (
                original_state["nspacl_was_null"]
                if original_state
                else True
            ),
            "original_acl_fingerprint": original_acl_fingerprint,
            "had_usage_before": bool(
                original_state
                and original_state["current_role_has_usage"]
            ),
            "had_create_before": bool(
                original_state
                and original_state["current_role_has_create"]
            ),
            "expected_acl_operation_count": len(
                _RB1L7_ACL_OPERATIONS
            ),
        },
    )


def _rb1l7_prepare_0020_shared_infrastructure():
    bind = _rb1l7_bind()
    identity = _rb1l7_require_migration_owner(bind)
    _rb1l7_validate_0020_roles(bind)

    original_state = _rb1l7_schema_state(
        bind,
        "app_private",
    )
    existed_before = original_state is not None

    if existed_before:
        if _rb1l7_header_exists(bind) or _rb1l7_marker_exists(bind):
            raise RuntimeError(
                "app_private contains an RB1L7 marker-name collision."
            )
        if not (
            original_state["current_role_has_usage"]
            and original_state["current_role_has_create"]
        ):
            raise RuntimeError(
                "Preexisting app_private is incompatible: migration "
                "role lacks USAGE/CREATE and ownership must not change."
            )
        original_acl_fingerprint = _rb1l7_acl_fingerprint(
            _rb1l7_all_direct_acl_rows(bind, "app_private")
        )
    else:
        original_acl_fingerprint = _rb1l7_acl_fingerprint([])
        bind.execute(
            sa.text(
                "CREATE SCHEMA app_private "
                "AUTHORIZATION migration_owner"
            )
        )

    prepared_state = _rb1l7_schema_state(
        bind,
        "app_private",
    )
    if prepared_state is None:
        raise RuntimeError("app_private preparation failed.")

    if existed_before and (
        prepared_state["schema_oid"] != original_state["schema_oid"]
        or prepared_state["owner_oid"] != original_state["owner_oid"]
    ):
        raise RuntimeError(
            "Preexisting app_private OID or owner changed."
        )

    _rb1l7_create_header_marker(
        bind,
        existed_before=existed_before,
        original_state=original_state,
        prepared_state=prepared_state,
        original_acl_fingerprint=original_acl_fingerprint,
        identity=identity,
    )
    _rb1l7_create_acl_marker(bind)
    _rb1l7_apply_acl_operations(bind)


def _rb1l7_insert_temp_marker(
    bind,
    *,
    before_rows,
    original_row,
    restoration,
    mutation_applied,
    added_by_revision,
    resulting_row,
    post_rows,
):
    _rb1l7_insert_acl_marker(
        bind,
        ordinal=_RB1L7_TEMP_OPERATION_ORDINAL,
        action="TEMPORARY_GRANT",
        schema_name="app_private",
        grantee_name="app_rls_executor",
        privilege_type="CREATE",
        before_rows=before_rows,
        original_row=original_row,
        restoration=restoration,
        mutation_applied=mutation_applied,
        added_by_revision=added_by_revision,
        removed_by_revision=False,
        resulting_row=resulting_row,
        post_rows=post_rows,
    )



def _rb1l7_prepare_temporary_app_private_create():
    global _RB1L7_TEMP_CREATE_PRESTATE
    global _RB1L7_TEMP_CREATE_ADDED_ROW
    global _RB1L7_TEMP_CREATE_PREPARED

    if _RB1L7_TEMP_CREATE_PREPARED:
        raise RuntimeError(
            "Temporary CREATE pre-state was captured more than once."
        )

    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    before_rows = _rb1l7_direct_acl_rows(
        bind,
        "app_private",
        "app_rls_executor",
        "CREATE",
    )
    _RB1L7_TEMP_CREATE_PRESTATE = tuple(
        MappingProxyType(dict(row))
        for row in before_rows
    )
    _RB1L7_TEMP_CREATE_ADDED_ROW = None
    _RB1L7_TEMP_CREATE_PREPARED = True

    if not before_rows:
        return

    for original_row in before_rows:
        _rb1l7_insert_temp_marker(
            bind,
            before_rows=before_rows,
            original_row=original_row,
            restoration=None,
            mutation_applied=False,
            added_by_revision=False,
            resulting_row=original_row,
            post_rows=before_rows,
        )




def _rb1l7_execute_temporary_create_grant(
    delegate,
    sql,
    args,
    kwargs,
):
    _rb1l7_require_migration_owner(_rb1l7_bind())
    global _RB1L7_TEMP_CREATE_ADDED_ROW

    if (
        not _RB1L7_TEMP_CREATE_PREPARED
        or _RB1L7_TEMP_CREATE_PRESTATE is None
    ):
        raise RuntimeError(
            "Temporary CREATE grant reached before pre-state capture."
        )

    if _RB1L7_TEMP_CREATE_PRESTATE:
        return None

    result = delegate.execute(sql, *args, **kwargs)
    bind = _rb1l7_bind()
    after_rows = _rb1l7_direct_acl_rows(
        bind,
        "app_private",
        "app_rls_executor",
        "CREATE",
    )
    if len(after_rows) != 1:
        raise RuntimeError(
            "Temporary CREATE did not produce exactly one direct row."
        )

    added_row = after_rows[0]
    restoration = _rb1l7_restoration_context(
        bind,
        added_row,
    )
    _RB1L7_TEMP_CREATE_ADDED_ROW = MappingProxyType(
        dict(added_row)
    )
    _rb1l7_insert_temp_marker(
        bind,
        before_rows=(),
        original_row=None,
        restoration=restoration,
        mutation_applied=True,
        added_by_revision=True,
        resulting_row=added_row,
        post_rows=after_rows,
    )
    return result


def _rb1l7_execute_temporary_create_revoke(
    delegate,
    sql,
    args,
    kwargs,
):
    _rb1l7_require_migration_owner(_rb1l7_bind())
    if (
        not _RB1L7_TEMP_CREATE_PREPARED
        or _RB1L7_TEMP_CREATE_PRESTATE is None
    ):
        raise RuntimeError(
            "Temporary CREATE revoke reached before pre-state capture."
        )

    if _RB1L7_TEMP_CREATE_ADDED_ROW is None:
        current_rows = _rb1l7_direct_acl_rows(
            _rb1l7_bind(),
            "app_private",
            "app_rls_executor",
            "CREATE",
        )
        if {
            _rb1l7_acl_tuple(row)
            for row in current_rows
        } != {
            _rb1l7_acl_tuple(row)
            for row in _RB1L7_TEMP_CREATE_PRESTATE
        }:
            raise RuntimeError(
                "Preexisting temporary CREATE ACL rows changed "
                "before the conditional revoke boundary."
            )
        return None

    result = delegate.execute(sql, *args, **kwargs)
    current_rows = _rb1l7_direct_acl_rows(
        _rb1l7_bind(),
        "app_private",
        "app_rls_executor",
        "CREATE",
    )
    if {
        _rb1l7_acl_tuple(row)
        for row in current_rows
    } != {
        _rb1l7_acl_tuple(row)
        for row in _RB1L7_TEMP_CREATE_PRESTATE
    }:
        raise RuntimeError(
            "Temporary CREATE revoke did not restore exact pre-state."
        )
    return result


def _rb1l7_restore_temporary_app_private_create():
    if (
        not _RB1L7_TEMP_CREATE_PREPARED
        or _RB1L7_TEMP_CREATE_PRESTATE is None
    ):
        raise RuntimeError(
            "Temporary CREATE pre-state was not captured."
        )

    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    current_rows = _rb1l7_direct_acl_rows(
        bind,
        "app_private",
        "app_rls_executor",
        "CREATE",
    )
    if {
        _rb1l7_acl_tuple(row)
        for row in current_rows
    } != {
        _rb1l7_acl_tuple(row)
        for row in _RB1L7_TEMP_CREATE_PRESTATE
    }:
        raise RuntimeError(
            "Temporary app_private CREATE direct pre-state "
            "was not restored exactly."
        )


def _rb1l7_0020_digest(bind):
    header = _rb1l7_fetch_one(
        bind,
        "SELECT * FROM " + _RB1L7_HEADER_MARKER,
    )
    rows = _rb1l7_marker_payload(bind)
    if header is None:
        raise RuntimeError("0020 lifecycle header is absent.")

    header_payload = dict(header)
    header_payload.pop("state_digest", None)
    header_payload.pop("state_finalized", None)
    header_payload.pop("captured_at", None)

    acl_payload = []
    for row in rows:
        item = dict(row)
        item.pop("state_digest", None)
        item.pop("state_finalized", None)
        item.pop("captured_at", None)
        acl_payload.append(item)

    return hashlib.sha256(
        json.dumps(
            {
                "header": header_payload,
                "acl_rows": acl_payload,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rb1l7_finalize_0020_markers():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    rows = _rb1l7_marker_payload(bind)
    ordinals = {
        int(row["operation_ordinal"])
        for row in rows
    }
    expected = set(
        range(1, len(_RB1L7_ACL_OPERATIONS) + 1)
    ) | {_RB1L7_TEMP_OPERATION_ORDINAL}
    if ordinals != expected:
        raise RuntimeError(
            "0020 ACL marker operation ordinals are incomplete."
        )

    digest = _rb1l7_0020_digest(bind)
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
    bind.execute(
        sa.text(
            "UPDATE "
            + _RB1L7_HEADER_MARKER
            + """
              SET state_finalized = TRUE,
                  state_digest = :digest
             WHERE singleton = TRUE
            """
        ),
        {"digest": digest},
    )


def _rb1l7_load_and_validate_0020_state():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    if not _rb1l7_header_exists(bind) or not _rb1l7_marker_exists(bind):
        raise RuntimeError("Required 0020 markers are absent.")

    header_rows = _rb1l7_fetch_all(
        bind,
        "SELECT * FROM " + _RB1L7_HEADER_MARKER,
    )
    if len(header_rows) != 1:
        raise RuntimeError(
            "0020 lifecycle marker singleton count is invalid."
        )
    header = header_rows[0]
    acl_rows = _rb1l7_marker_payload(bind)

    if (
        int(header["marker_version"]) != _RB1L7_MARKER_VERSION
        or header["revision"] != _RB1L7_REVISION
        or int(header["expected_acl_operation_count"])
        != len(_RB1L7_ACL_OPERATIONS)
        or not header["state_finalized"]
        or not header["state_digest"]
    ):
        raise RuntimeError(
            "0020 lifecycle marker version/count/finalized state "
            "is invalid."
        )

    ordinals = {
        int(row["operation_ordinal"])
        for row in acl_rows
    }
    expected = set(
        range(1, len(_RB1L7_ACL_OPERATIONS) + 1)
    ) | {_RB1L7_TEMP_OPERATION_ORDINAL}
    if ordinals != expected:
        raise RuntimeError(
            "0020 ACL marker operation ordinals are invalid."
        )

    if any(
        int(row["marker_version"]) != _RB1L7_MARKER_VERSION
        or row["revision"] != _RB1L7_REVISION
        or int(row["expected_operation_count"])
        != len(_RB1L7_ACL_OPERATIONS)
        or not row["state_finalized"]
        or not row["state_digest"]
        for row in acl_rows
    ):
        raise RuntimeError(
            "0020 ACL marker version/count/finalized state "
            "is invalid."
        )

    digests = {
        row["state_digest"]
        for row in acl_rows
    } | {header["state_digest"]}
    if len(digests) != 1:
        raise RuntimeError("0020 marker digest values disagree.")

    if _rb1l7_0020_digest(bind) != header["state_digest"]:
        raise RuntimeError("0020 marker digest verification failed.")

    current = _rb1l7_schema_state(bind, "app_private")
    if (
        current is None
        or int(current["schema_oid"])
        != int(header["app_private_oid_after_prepare"])
        or int(current["owner_oid"])
        != int(header["owner_after_prepare_oid"])
        or current["owner_name"]
        != header["owner_after_prepare_name"]
    ):
        raise RuntimeError(
            "app_private OID/owner differs from lifecycle marker."
        )

    return MappingProxyType(
        {
            "header": MappingProxyType(dict(header)),
            "acl_rows": tuple(
                MappingProxyType(dict(row))
                for row in acl_rows
            ),
        }
    )


def _rb1l7_verify_temporary_create_from_state(state):
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    rows = [
        row
        for row in state["acl_rows"]
        if int(row["operation_ordinal"])
        == _RB1L7_TEMP_OPERATION_ORDINAL
    ]
    expected = {
        (
            int(row["schema_oid"]),
            int(row["schema_owner_oid"]),
            int(row["original_grantor_oid"]),
            int(row["grantee_oid"]),
            str(row["privilege_type"]),
            bool(row["original_is_grantable"]),
        )
        for row in rows
        if row["direct_row_existed"]
    }
    current = {
        _rb1l7_acl_tuple(row)
        for row in _rb1l7_direct_acl_rows(
            bind,
            "app_private",
            "app_rls_executor",
            "CREATE",
        )
    }
    if current != expected:
        raise RuntimeError(
            "Temporary CREATE direct ACL state leaked or changed."
        )


def _rb1l7_restore_0020_acl_state(state):
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    _rb1l7_verify_temporary_create_from_state(state)
    persistent = tuple(
        row
        for row in state["acl_rows"]
        if int(row["operation_ordinal"])
        <= len(_RB1L7_ACL_OPERATIONS)
    )
    _rb1l7_restore_acl_rows(
        bind,
        persistent,
    )



def _rb1l7_drop_0020_markers_and_maybe_schema(state):
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    header = state["header"]

    bind.execute(
        sa.text(
            "DROP TABLE "
            + _RB1L7_ACL_MARKER
            + " RESTRICT"
        )
    )
    bind.execute(
        sa.text(
            "DROP TABLE "
            + _RB1L7_HEADER_MARKER
            + " RESTRICT"
        )
    )

    if not header["app_private_created_by_revision"]:
        current = _rb1l7_schema_state(bind, "app_private")
        if (
            current is None
            or int(current["schema_oid"])
            != int(header["app_private_oid_after_prepare"])
            or int(current["owner_oid"])
            != int(header["original_owner_oid"])
            or current["owner_name"]
            != header["original_owner_name"]
        ):
            raise RuntimeError(
                "Preexisting app_private OID/owner was not preserved."
            )
        return

    bind.execute(
        sa.text("DROP SCHEMA app_private RESTRICT")
    )

# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_END


_0020_DOMAIN_TYPES = (
    "public.contact_kind_enum",
    "public.visibility_scope_enum",
    "public.audit_action_enum",
    "public.verification_method_enum",
)
_0020_PUBLIC_RELATIONS = (
    "branch_contacts",
    "branch_contacts_audit",
    "branch_contacts_audit_default",
    "ix_contacts_org_branch_active",
    "ix_active_branch_contacts",
    "ix_public_contacts",
    "ix_primary_contact_lookup",
    "ix_contacts_search_phone",
    "ix_contacts_search_email",
    "ix_branch_contacts_primary_ordered",
    "uq_public_primary_phone",
    "uq_public_primary_email",
    "uq_primary_contact_guard_idx",
    "ix_audit_contact",
    "ix_audit_branch_contacts_ordered",
    "ix_audit_org_changed",
)
_0020_PRIVATE_RELATIONS = ("partition_metadata",)
_0020_PRIVATE_FUNCTIONS = (
    "app_private.prevent_soft_delete_resurrection()",
    "app_private.prevent_audit_modification()",
    "app_private.update_timestamp()",
    "app_private.log_branch_contact_changes()",
    "app_private.process_primary_contact_batch(uuid[])",
    "app_private.ensure_primary_contact_insert()",
    "app_private.ensure_primary_contact_update()",
    "app_private.ensure_primary_contact_delete()",
    "app_private.create_branch_contacts_audit_partition(date)",
)
_0020_TRIGGERS = (
    ("branch_contacts", "trg_prevent_soft_delete_resurrection"),
    ("branch_contacts_audit", "trg_prevent_audit_update"),
    ("branch_contacts", "trg_branch_contacts_updated_at"),
    ("branch_contacts", "trg_audit_branch_contacts"),
    ("branch_contacts", "trg_ensure_primary_contact_insert"),
    ("branch_contacts", "trg_ensure_primary_contact_update"),
    ("branch_contacts", "trg_ensure_primary_contact_delete"),
)
_0020_POLICIES = (
    ("branch_contacts", "tenant_isolation_contacts"),
    ("branch_contacts_audit", "tenant_isolation_contacts_audit"),
)


def _0020_require_citext_infrastructure(bind):
    row = _rb1l7_fetch_one(
        bind,
        """
        SELECT
            owner_role.rolname::text AS owner_name,
            namespace_data.nspname::text AS schema_name,
            extension_data.extversion::text AS extension_version
        FROM pg_catalog.pg_extension AS extension_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = extension_data.extowner
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = extension_data.extnamespace
        WHERE extension_data.extname = 'citext'
        """,
    )
    if (
        row is None
        or row["owner_name"] != "postgres"
        or row["schema_name"] != "public"
    ):
        raise RuntimeError(
            "0020 requires infrastructure-owned citext in public "
            "with owner postgres; Alembic must not create/adopt it."
        )


def _0020_preflight_upgrade_domain():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    _0020_require_citext_infrastructure(bind)

    collisions = []
    for qualified_name in _0020_DOMAIN_TYPES:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regtype(:name) IS NOT NULL AS present",
            {"name": qualified_name},
        )
        if row and row["present"]:
            collisions.append(qualified_name)

    for relation_name in _0020_PUBLIC_RELATIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
            {"name": "public." + relation_name},
        )
        if row and row["present"]:
            collisions.append("public." + relation_name)

    for relation_name in _0020_PRIVATE_RELATIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
            {"name": "app_private." + relation_name},
        )
        if row and row["present"]:
            collisions.append("app_private." + relation_name)

    for function_name in _0020_PRIVATE_FUNCTIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regprocedure(:name) IS NOT NULL AS present",
            {"name": function_name},
        )
        if row and row["present"]:
            collisions.append(function_name)

    if collisions:
        raise RuntimeError(
            "0020 target-object collision; refusing silent adoption: "
            + ", ".join(sorted(collisions))
        )


def _0020_assert_downgrade_domain_shape(bind):
    _rb1l7_require_migration_owner(bind)
    _0020_require_citext_infrastructure(bind)

    missing = []
    for qualified_name in _0020_DOMAIN_TYPES:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regtype(:name) IS NOT NULL AS present",
            {"name": qualified_name},
        )
        if not row or not row["present"]:
            missing.append(qualified_name)

    for relation_name in _0020_PUBLIC_RELATIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
            {"name": "public." + relation_name},
        )
        if not row or not row["present"]:
            missing.append("public." + relation_name)

    for relation_name in _0020_PRIVATE_RELATIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regclass(:name) IS NOT NULL AS present",
            {"name": "app_private." + relation_name},
        )
        if not row or not row["present"]:
            missing.append("app_private." + relation_name)

    for function_name in _0020_PRIVATE_FUNCTIONS:
        row = _rb1l7_fetch_one(
            bind,
            "SELECT pg_catalog.to_regprocedure(:name) IS NOT NULL AS present",
            {"name": function_name},
        )
        if not row or not row["present"]:
            missing.append(function_name)

    for relation_name, trigger_name in _0020_TRIGGERS:
        row = _rb1l7_fetch_one(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_trigger AS trigger_data
                JOIN pg_catalog.pg_class AS relation_data
                  ON relation_data.oid = trigger_data.tgrelid
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :relation_name
                  AND trigger_data.tgname = :trigger_name
                  AND NOT trigger_data.tgisinternal
            ) AS present
            """,
            {"relation_name": relation_name, "trigger_name": trigger_name},
        )
        if not row or not row["present"]:
            missing.append("trigger:" + trigger_name)

    for relation_name, policy_name in _0020_POLICIES:
        row = _rb1l7_fetch_one(
            bind,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_data
                JOIN pg_catalog.pg_class AS relation_data
                  ON relation_data.oid = policy_data.polrelid
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :relation_name
                  AND policy_data.polname = :policy_name
            ) AS present
            """,
            {"relation_name": relation_name, "policy_name": policy_name},
        )
        if not row or not row["present"]:
            missing.append("policy:" + policy_name)

    fk = _rb1l7_fetch_one(
        bind,
        """
        SELECT constraint_data.convalidated AS validated
        FROM pg_catalog.pg_constraint AS constraint_data
        JOIN pg_catalog.pg_class AS relation_data
          ON relation_data.oid = constraint_data.conrelid
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = relation_data.relnamespace
        WHERE namespace_data.nspname = 'public'
          AND relation_data.relname = 'branch_contacts_audit'
          AND constraint_data.conname = 'fk_branch_contacts_audit_org'
          AND constraint_data.contype = 'f'
        """,
    )
    if fk is None or not fk["validated"]:
        missing.append("constraint:fk_branch_contacts_audit_org(validated)")

    if missing:
        raise RuntimeError(
            "0020 downgrade domain-shape drift before mutation: "
            + ", ".join(sorted(missing))
        )


def _0020_preflight_downgrade_domain():
    bind = _rb1l7_bind()
    _rb1l7_require_migration_owner(bind)
    _0020_assert_downgrade_domain_shape(bind)

    org_rows = _rb1l7_fetch_all(
        bind,
        "SELECT id::text AS org_id FROM public.organizations ORDER BY id",
    )

    bind.execute(sa.text("SET LOCAL ROLE app_rls_executor"))
    try:
        for org_row in org_rows:
            org_id = str(org_row["org_id"])
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.set_config("
                    "'app.current_org_id', :org_id, true)"
                ),
                {"org_id": org_id},
            )
            for relation_name in ("branch_contacts", "branch_contacts_audit"):
                row = _rb1l7_fetch_one(
                    bind,
                    "SELECT EXISTS (SELECT 1 FROM public."
                    + relation_name
                    + " WHERE org_id = :org_id LIMIT 1) AS present",
                    {"org_id": org_id},
                )
                if row and row["present"]:
                    raise RuntimeError(
                        "0020 downgrade would discard populated business/audit "
                        f"relation public.{relation_name} for organization {org_id}"
                    )

        unexpected_metadata = _rb1l7_fetch_one(
            bind,
            """
            SELECT table_name, partition_name
            FROM app_private.partition_metadata
            WHERE table_name <> 'branch_contacts_audit'
            ORDER BY table_name, partition_name
            LIMIT 1
            """,
        )
        if unexpected_metadata is not None:
            raise RuntimeError(
                "0020 downgrade refuses partition_metadata owned by another "
                "table: " + str(unexpected_metadata)
            )

        orphan_metadata = _rb1l7_fetch_one(
            bind,
            """
            SELECT metadata.partition_name
            FROM app_private.partition_metadata AS metadata
            WHERE metadata.table_name = 'branch_contacts_audit'
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_inherits AS inheritance_data
                  JOIN pg_catalog.pg_class AS child_data
                    ON child_data.oid = inheritance_data.inhrelid
                  JOIN pg_catalog.pg_class AS parent_data
                  ON parent_data.oid = inheritance_data.inhparent
                  JOIN pg_catalog.pg_namespace AS child_namespace
                  ON child_namespace.oid = child_data.relnamespace
                  JOIN pg_catalog.pg_namespace AS parent_namespace
                    ON parent_namespace.oid = parent_data.relnamespace
                  WHERE child_namespace.nspname = 'public'
                    AND parent_namespace.nspname = 'public'
                    AND parent_data.relname = 'branch_contacts_audit'
                    AND child_data.relname = metadata.partition_name
              )
            ORDER BY metadata.partition_name
            LIMIT 1
            """,
       )
        if orphan_metadata is not None:
            raise RuntimeError(
                "0020 downgrade found partition metadata without attached "
                "partition: " + str(orphan_metadata["partition_name"])
            )

        untracked_partition = _rb1l7_fetch_one(
            bind,
            """
            SELECT child_data.relname::text AS partition_name
            FROM pg_catalog.pg_inherits AS inheritance_data
            JOIN pg_catalog.pg_class AS child_data
              ON child_data.oid = inheritance_data.inhrelid
            JOIN pg_catalog.pg_class AS parent_data
              ON parent_data.oid = inheritance_data.inhparent
            JOIN pg_catalog.pg_namespace AS child_namespace
              ON child_namespace.oid = child_data.relnamespace
            JOIN pg_catalog.pg_namespace AS parent_namespace
              ON parent_namespace.oid = parent_data.relnamespace
            WHERE child_namespace.nspname = 'public'
              AND parent_namespace.nspname = 'public'
              AND parent_data.relname = 'branch_contacts_audit'
              AND child_data.relname <> 'branch_contacts_audit_default'
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_private.partition_metadata AS metadata
                  WHERE metadata.table_name = 'branch_contacts_audit'
                    AND metadata.partition_name = child_data.relname
              )
            ORDER BY child_data.relname
            LIMIT 1
            """,
        )
        if untracked_partition is not None:
            raise RuntimeError(
                "0020 downgrade found untracked audit partition: "
                + str(untracked_partition["partition_name"])
            )

        bind.execute(
            sa.text(
                "SELECT pg_catalog.set_config("
                "'app.current_org_id', "
                "'00000000-0000-0000-0000-000000000000', true)"
            )
        )
    finally:
        bind.execute(sa.text("RESET ROLE"))
        _rb1l7_require_migration_owner(bind)


def upgrade():
    """
    Phase A Deployment: Schema Creation + NOT VALID Constraints
    Downtime: 0 seconds (safe to run during business hours)
    Risk Level: Very Low (no existing data affected)
    """

    # ===========================================================================
    # SECTION 1: Extensions & Types
    # ===========================================================================
    op = _rb1l7_upgrade_operations(globals()["op"])
    _0020_preflight_upgrade_domain()
    _rb1l7_prepare_0020_shared_infrastructure()

    # Custom types are revision-owned; collisions are rejected by preflight.
    op.execute("CREATE TYPE public.contact_kind_enum AS ENUM ('phone', 'email');")
    op.execute(
        "CREATE TYPE public.visibility_scope_enum AS ENUM "
        "('public', 'internal', 'management', 'emergency', 'billing');"
    )
    op.execute(
        "CREATE TYPE public.audit_action_enum AS ENUM ('INSERT', 'UPDATE', 'DELETE');"
    )
    op.execute(
        "CREATE TYPE public.verification_method_enum AS ENUM "
        "('dns_mx', 'manual', 'smtp_probe', 'twilio_verify');"
    )

    # ===========================================================================
    # SECTION 2: Externally managed cluster-role validation
    # ===========================================================================
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM (VALUES
                ('app_rls_executor'),
                ('app_user')
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
                WHERE role_data.rolname IN ('app_rls_executor', 'app_user')
                  AND (
                        role_data.rolsuper
                     OR role_data.rolbypassrls
                     OR role_data.rolcanlogin
                     OR role_data.rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'Managed cluster role attributes violate the approved security/cluster_role_bootstrap contract must be applied before Alembic migrations.';
            END IF;
        END
        $$;
    """)



    # ===========================================================================
    # SECTION 3: Main Contacts Table
    # ===========================================================================
    op.execute("""
        CREATE TABLE public.branch_contacts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            contact_kind public.contact_kind_enum NOT NULL,

            -- Normalized phone data
            phone_e164 VARCHAR(20),
            normalized_digits VARCHAR(20),
            display_format VARCHAR(100),

            -- Dual email strategy (raw + normalized for display vs. indexing)
            email_raw VARCHAR(255),
            email_normalized CITEXT,

            country_code CHAR(2),

            contact_label VARCHAR(50) NOT NULL DEFAULT 'General',
            visibility_scope public.visibility_scope_enum NOT NULL DEFAULT 'internal',

            -- Channel capabilities as JSONB
            channel_capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,

            -- Generated column for optimized whatsapp lookups
            is_whatsapp_enabled BOOLEAN GENERATED ALWAYS AS (
                COALESCE((channel_capabilities->>'whatsapp')::boolean, FALSE)
            ) STORED,

            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            email_reachability_verified BOOLEAN NOT NULL DEFAULT FALSE,

            -- Verification metadata
            verified_at TIMESTAMPTZ,
            verification_method public.verification_method_enum,

            -- Temporal soft-delete fields
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by UUID,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by UUID,
            deleted_at TIMESTAMPTZ,
            deleted_by UUID,

            -- Primary contact guard (for efficient unique constraint)
            primary_guard UUID GENERATED ALWAYS AS (
                CASE
                    WHEN is_primary = TRUE
                     AND is_active = TRUE
                     AND deleted_at IS NULL
                    THEN branch_id
                END
            ) STORED
        );
    """)

    # Operational tuning (HOT-aware, soft-delete aware)
    op.execute("""
        ALTER TABLE public.branch_contacts SET (
            fillfactor = 85,
            autovacuum_vacuum_scale_factor = 0.05,
            autovacuum_analyze_scale_factor = 0.02
        );
    """)

    # CRITICAL: Privilege hardening; ownership is transferred in Section 11.
    op.execute("REVOKE ALL ON public.branch_contacts FROM PUBLIC;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.branch_contacts TO app_user;")
    op.execute("REVOKE DELETE ON public.branch_contacts FROM PUBLIC;")

    # ===========================================================================
    # SECTION 4: Audit Table with Time Partitioning
    # ===========================================================================
    op.execute("""
        CREATE TABLE public.branch_contacts_audit (
            id UUID DEFAULT gen_random_uuid(),
            changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            org_id UUID NOT NULL,
            branch_contact_id UUID NOT NULL,
            changed_by UUID,
            action public.audit_action_enum NOT NULL,
            changed_fields JSONB NOT NULL,
            request_id UUID,
            ip_address INET,
            user_agent TEXT,
            change_reason VARCHAR(500),
            PRIMARY KEY (changed_at, id)
        ) PARTITION BY RANGE (changed_at);
    """)

    # The tenant registry must be authoritative for rollback preflight.
    # This validated FK prevents orphan audit tenant IDs from becoming invisible
    # to the tenant-by-tenant FORCE-RLS scan used by downgrade.
    op.execute("""
        ALTER TABLE public.branch_contacts_audit
        ADD CONSTRAINT fk_branch_contacts_audit_org
        FOREIGN KEY (org_id)
        REFERENCES public.organizations(id)
        ON DELETE RESTRICT;
    """)

    # Default partition for seamless insertions
    op.execute("""
        CREATE TABLE public.branch_contacts_audit_default
        PARTITION OF public.branch_contacts_audit DEFAULT;
    """)

    # LZ4 compression for JSONB payloads
    op.execute("""
        ALTER TABLE public.branch_contacts_audit ALTER COLUMN changed_fields
        SET COMPRESSION lz4;
    """)

    # Audit table operational tuning (Leaf default partition)
    op.execute("""
        ALTER TABLE public.branch_contacts_audit_default SET (
            fillfactor = 100,
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_analyze_scale_factor = 0.01
        );
    """)

    # Explicit default-partition parity. Parent-level hardening does not
    # recursively apply ownership, RLS flags, privileges, or compression.
    op.execute(
        "ALTER TABLE public.branch_contacts_audit_default "
        "ALTER COLUMN changed_fields SET COMPRESSION lz4;"
    )
    op.execute(
        "ALTER TABLE public.branch_contacts_audit_default "
        "ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_contacts_audit_default "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "GRANT SELECT, INSERT ON "
        "public.branch_contacts_audit_default TO app_user;"
    )

    # Privilege hardening; ownership is transferred in Section 11.
    op.execute("REVOKE ALL ON public.branch_contacts_audit FROM PUBLIC;")
    op.execute("GRANT SELECT, INSERT ON public.branch_contacts_audit TO app_user;")

    # ===========================================================================
    # SECTION 5: RLS Policies - Multi-tenant Isolation (Non-negotiable)
    # ===========================================================================
    op.execute("ALTER TABLE public.branch_contacts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_contacts FORCE ROW LEVEL SECURITY;")

    op.execute("ALTER TABLE public.branch_contacts_audit ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_contacts_audit FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_contacts ON public.branch_contacts
            FOR ALL
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    op.execute("""
        CREATE POLICY tenant_isolation_contacts_audit ON public.branch_contacts_audit
            FOR ALL
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    # ===========================================================================
    # SECTION 6: Constraints (NOT VALID Strategy)
    # All constraints added as NOT VALID for zero-downtime rollout.
    # Validation happens async in Phase C.
    # ===========================================================================

    # Foreign Key Protection
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT fk_branch_contacts_org_branch
            FOREIGN KEY (branch_id, org_id)
            REFERENCES public.org_branches(id, org_id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    # XOR Constraint: phone XOR email
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_contact_kind_fields CHECK (
            (contact_kind = 'phone' AND phone_e164 IS NOT NULL
                AND normalized_digits IS NOT NULL AND country_code IS NOT NULL
                AND email_normalized IS NULL AND email_raw IS NULL) OR
            (contact_kind = 'email' AND email_normalized IS NOT NULL
                AND email_raw IS NOT NULL AND phone_e164 IS NULL
                AND normalized_digits IS NULL AND country_code IS NULL)
        ) NOT VALID;
    """)

    # Email verification strictness
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_email_verification_email_only CHECK (
            contact_kind = 'email' OR
            (email_reachability_verified = FALSE AND verified_at IS NULL
                AND verification_method IS NULL)
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_verification_metadata CHECK (
            (verified_at IS NULL AND verification_method IS NULL) OR
            (verified_at IS NOT NULL AND verification_method IS NOT NULL)
        ) NOT VALID;
    """)

    # JSONB deep validation
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_channel_capabilities_schema CHECK (
            jsonb_typeof(channel_capabilities) = 'object'
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_channel_capabilities_values CHECK (
            (NOT (channel_capabilities ? 'whatsapp')
                OR jsonb_typeof(channel_capabilities->'whatsapp') = 'boolean') AND
            (NOT (channel_capabilities ? 'sms')
                OR jsonb_typeof(channel_capabilities->'sms') = 'boolean') AND
            (NOT (channel_capabilities ? 'voice')
                OR jsonb_typeof(channel_capabilities->'voice') = 'boolean') AND
            (NOT (channel_capabilities ? 'fax')
                OR jsonb_typeof(channel_capabilities->'fax') = 'boolean')
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_channel_capability_allowed_keys CHECK (
            channel_capabilities - ARRAY['whatsapp','sms','voice','fax'] = '{}'::jsonb
        ) NOT VALID;
    """)

    # Payload size protection
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_channel_capabilities_size CHECK (
            pg_column_size(channel_capabilities) <= 1024
        ) NOT VALID;
    """)

    # Format validation
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_phone_e164_format CHECK (
            phone_e164 IS NULL OR phone_e164 ~ '^\\+[1-9]\\d{1,14}$'
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_normalized_digits_numeric CHECK (
            normalized_digits IS NULL OR normalized_digits ~ '^[0-9]+$'
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_email_not_empty CHECK (
            email_normalized IS NULL OR length(trim(email_normalized::text)) > 0
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_display_format_required CHECK (
            contact_kind != 'phone' OR display_format IS NOT NULL
        ) NOT VALID;
    """)

    # Logical invariants
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_primary_requires_active CHECK (
            NOT (is_primary = TRUE AND is_active = FALSE)
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_deleted_rows_inactive CHECK (
            deleted_at IS NULL OR is_active = FALSE
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_deleted_rows_not_primary CHECK (
            deleted_at IS NULL OR is_primary = FALSE
        ) NOT VALID;
    """)

    # NO-RESURRECTION constraint: immutable soft-delete
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_deleted_immutable CHECK (
            deleted_at IS NULL OR deleted_by IS NOT NULL
        ) NOT VALID;
    """)

    # Metadata completeness
    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_deleted_metadata CHECK (
            (deleted_at IS NULL AND deleted_by IS NULL) OR
            (deleted_at IS NOT NULL AND deleted_by IS NOT NULL)
        ) NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_contacts
        ADD CONSTRAINT chk_updated_metadata CHECK (
            updated_at >= created_at
        ) NOT VALID;
    """)

    # ===========================================================================
    # SECTION 7: Functions - HARDENED with Minimal search_path
    # ===========================================================================

    # PREVENT SOFT DELETE RESURRECTION
    op.execute("""
        CREATE FUNCTION app_private.prevent_soft_delete_resurrection()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        BEGIN
            IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
                RAISE EXCEPTION
                    'Branch contacts cannot be undeleted (deleted_at is immutable once set). '
                    'The system treats deletions as permanent. '
                    'To reactivate, insert a new contact record with is_primary reassessment.';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.prevent_soft_delete_resurrection() FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.prevent_soft_delete_resurrection() TO app_rls_executor;")

    # PREVENT AUDIT MODIFICATION
    op.execute("""
        CREATE FUNCTION app_private.prevent_audit_modification()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        BEGIN
            RAISE EXCEPTION 'Audit table is strictly append-only';
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.prevent_audit_modification() FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.prevent_audit_modification() TO app_rls_executor;")

    # UPDATE TIMESTAMP TRIGGER (HOT-Optimized)
    op.execute("""
        CREATE FUNCTION app_private.update_timestamp()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        BEGIN
            -- ⚠️  WARNING: Future Schema Maintainers
            -- This trigger uses EXPLICIT field comparison for HOT performance optimization.
            -- DO NOT refactor to structural JSONB diffs without performance testing.
            -- DO NOT add new business-logic columns without:
            --   1. Confirming it should trigger timestamp updates
            --   2. Load testing impact
            --   3. Documenting decision in git commit
            -- Contact SRE team before modifying this trigger.

            IF (
                NEW.phone_e164 IS DISTINCT FROM OLD.phone_e164 OR
                NEW.email_normalized IS DISTINCT FROM OLD.email_normalized OR
                NEW.email_raw IS DISTINCT FROM OLD.email_raw OR
                NEW.is_primary IS DISTINCT FROM OLD.is_primary OR
                NEW.is_active IS DISTINCT FROM OLD.is_active OR
                NEW.channel_capabilities IS DISTINCT FROM OLD.channel_capabilities OR
                NEW.contact_label IS DISTINCT FROM OLD.contact_label OR
                NEW.visibility_scope IS DISTINCT FROM OLD.visibility_scope OR
                NEW.deleted_at IS DISTINCT FROM OLD.deleted_at OR
                NEW.display_format IS DISTINCT FROM OLD.display_format
            ) THEN
                NEW.updated_at = CURRENT_TIMESTAMP;
                NEW.updated_by = NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.update_timestamp() FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.update_timestamp() TO app_rls_executor;")

    # LOG BRANCH CONTACT CHANGES (WAL-optimized audit)
    op.execute("""
        CREATE FUNCTION app_private.log_branch_contact_changes()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        DECLARE
            changed_by_id UUID := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            req_id UUID := NULLIF(current_setting('app.request_id', true), '')::UUID;
            ip_addr INET := NULLIF(current_setting('app.ip_address', true), '')::INET;
            ua TEXT := NULLIF(current_setting('app.user_agent', true), '');
            diff_json JSONB;
        BEGIN
            -- Prevent synthetic recursive noise during invariant auto-promotions
            IF current_setting('app.internal_maintenance', true) = 'on' THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END IF;

            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.branch_contacts_audit
                    (org_id, branch_contact_id, changed_by, action, changed_fields, request_id, ip_address, user_agent)
                VALUES (NEW.org_id, NEW.id, changed_by_id, 'INSERT',
                    jsonb_build_object(
                        'i', NEW.id, 'k', NEW.contact_kind,
                        'p', NEW.phone_e164, 'e', NEW.email_normalized,
                        's', NEW.visibility_scope, 'm', NEW.is_primary, 'a', NEW.is_active
                    ), req_id, ip_addr, ua);
            ELSIF TG_OP = 'UPDATE' THEN
                IF ROW(NEW.phone_e164, NEW.email_normalized, NEW.email_raw, NEW.is_primary,
                        NEW.is_active, NEW.deleted_at, NEW.visibility_scope, NEW.channel_capabilities,
                        NEW.contact_label, NEW.display_format)
                   IS NOT DISTINCT FROM
                   ROW(OLD.phone_e164, OLD.email_normalized, OLD.email_raw, OLD.is_primary,
                        OLD.is_active, OLD.deleted_at, OLD.visibility_scope, OLD.channel_capabilities,
                        OLD.contact_label, OLD.display_format) THEN
                    RETURN NEW;
                END IF;

                diff_json := jsonb_strip_nulls(jsonb_build_object(
                    'phone_e164', CASE WHEN NEW.phone_e164 IS DISTINCT FROM OLD.phone_e164
                        THEN jsonb_build_object('o', OLD.phone_e164, 'n', NEW.phone_e164) END,
                    'email_normalized', CASE WHEN NEW.email_normalized IS DISTINCT FROM OLD.email_normalized
                        THEN jsonb_build_object('o', OLD.email_normalized, 'n', NEW.email_normalized) END,
                    'email_raw', CASE WHEN NEW.email_raw IS DISTINCT FROM OLD.email_raw
                        THEN jsonb_build_object('o', OLD.email_raw, 'n', NEW.email_raw) END,
                    'is_primary', CASE WHEN NEW.is_primary IS DISTINCT FROM OLD.is_primary
                        THEN jsonb_build_object('o', OLD.is_primary, 'n', NEW.is_primary) END,
                    'is_active', CASE WHEN NEW.is_active IS DISTINCT FROM OLD.is_active
                        THEN jsonb_build_object('o', OLD.is_active, 'n', NEW.is_active) END,
                    'deleted_at', CASE WHEN NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
                        THEN jsonb_build_object('o', OLD.deleted_at, 'n', NEW.deleted_at) END,
                    'visibility_scope', CASE WHEN NEW.visibility_scope IS DISTINCT FROM OLD.visibility_scope
                        THEN jsonb_build_object('o', OLD.visibility_scope, 'n', NEW.visibility_scope) END,
                    'channel_capabilities', CASE WHEN NEW.channel_capabilities IS DISTINCT FROM OLD.channel_capabilities
                        THEN jsonb_build_object('o', OLD.channel_capabilities, 'n', NEW.channel_capabilities) END,
                    'contact_label', CASE WHEN NEW.contact_label IS DISTINCT FROM OLD.contact_label
                        THEN jsonb_build_object('o', OLD.contact_label, 'n', NEW.contact_label) END,
                    'display_format', CASE WHEN NEW.display_format IS DISTINCT FROM OLD.display_format
                        THEN jsonb_build_object('o', OLD.display_format, 'n', NEW.display_format) END
                ));

                IF diff_json <> '{}'::jsonb THEN
                    INSERT INTO public.branch_contacts_audit
                        (org_id, branch_contact_id, changed_by, action, changed_fields, request_id, ip_address, user_agent)
                    VALUES (NEW.org_id, NEW.id, changed_by_id, 'UPDATE', diff_json, req_id, ip_addr, ua);
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                INSERT INTO public.branch_contacts_audit
                    (org_id, branch_contact_id, changed_by, action, changed_fields, request_id, ip_address, user_agent)
                VALUES (OLD.org_id, OLD.id, changed_by_id, 'DELETE',
                    jsonb_build_object(
                        'i', OLD.id, 'k', OLD.contact_kind,
                        'p', OLD.phone_e164, 'e', OLD.email_normalized,
                        's', OLD.visibility_scope, 'm', OLD.is_primary, 'a', OLD.is_active
                    ), req_id, ip_addr, ua);
            END IF;

            IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.log_branch_contact_changes() FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.log_branch_contact_changes() TO app_rls_executor;")

    # PRIMARY CONTACT BATCH PROCESSOR (HARDENED with hashtextextended)
    op.execute("""
        CREATE FUNCTION app_private.process_primary_contact_batch(branches_to_check UUID[])
        RETURNS VOID
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        DECLARE
            v_branch UUID;
            v_candidate_id UUID;
            kind_val public.contact_kind_enum;
        BEGIN
            FOREACH v_branch IN ARRAY branches_to_check LOOP
                -- Native PostgreSQL hashing (40-60% cheaper than md5)
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(v_branch::text, 0)
                );

                FOREACH kind_val IN ARRAY ARRAY['phone'::public.contact_kind_enum, 'email'::public.contact_kind_enum] LOOP
                    IF NOT EXISTS (
                        SELECT 1 FROM public.branch_contacts
                        WHERE branch_id = v_branch
                          AND contact_kind = kind_val
                          AND is_primary = TRUE
                          AND is_active = TRUE
                          AND deleted_at IS NULL
                    ) THEN
                        -- DETERMINISTIC: Order by created_at ASC, id ASC
                        SELECT id INTO v_candidate_id FROM public.branch_contacts
                        WHERE branch_id = v_branch
                          AND contact_kind = kind_val
                          AND is_active = TRUE
                          AND deleted_at IS NULL
                        ORDER BY created_at ASC, id ASC
                        LIMIT 1;

                        IF v_candidate_id IS NOT NULL THEN
                            BEGIN
                                PERFORM set_config('app.internal_maintenance', 'on', true);

                                UPDATE public.branch_contacts
                                SET is_primary = TRUE
                                WHERE id = v_candidate_id AND is_primary = FALSE;

                                PERFORM set_config('app.internal_maintenance', 'off', true);
                            EXCEPTION WHEN OTHERS THEN
                                -- Connection-pool safe: explicit cleanup
                                PERFORM set_config('app.internal_maintenance', 'off', true);
                                RAISE LOG 'Primary contact batch failed for %: %', v_branch, SQLERRM;
                                RAISE;
                            END;
                        END IF;
                    END IF;
                END LOOP;
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.process_primary_contact_batch(UUID[]) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.process_primary_contact_batch(UUID[]) TO app_rls_executor;")

    # INSERT handler for primary contact invariant
    op.execute("""
        CREATE FUNCTION app_private.ensure_primary_contact_insert()
        RETURNS TRIGGER SECURITY DEFINER SET search_path = pg_catalog SET row_security = on AS $$
        BEGIN
            IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;
            PERFORM app_private.process_primary_contact_batch(
                ARRAY(SELECT DISTINCT branch_id FROM newly_inserted)
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.ensure_primary_contact_insert() FROM PUBLIC;")

    # UPDATE handler for primary contact invariant
    op.execute("""
        CREATE FUNCTION app_private.ensure_primary_contact_update()
        RETURNS TRIGGER SECURITY DEFINER SET search_path = pg_catalog SET row_security = on AS $$
        BEGIN
            IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;
            PERFORM app_private.process_primary_contact_batch(
                ARRAY(
                    SELECT DISTINCT branch_id FROM previously_updated
                    UNION
                    SELECT DISTINCT branch_id FROM newly_updated
                )
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.ensure_primary_contact_update() FROM PUBLIC;")

    # DELETE handler for primary contact invariant
    op.execute("""
        CREATE FUNCTION app_private.ensure_primary_contact_delete()
        RETURNS TRIGGER SECURITY DEFINER SET search_path = pg_catalog SET row_security = on AS $$
        BEGIN
            IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;
            PERFORM app_private.process_primary_contact_batch(
                ARRAY(SELECT DISTINCT branch_id FROM previously_deleted)
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.ensure_primary_contact_delete() FROM PUBLIC;")

    # ===========================================================================
    # SECTION 8: Triggers (Optimized with UPDATE OF Column Scoping)
    # ===========================================================================

    # Prevent resurrection
    op.execute("""
        CREATE TRIGGER trg_prevent_soft_delete_resurrection
            BEFORE UPDATE ON public.branch_contacts
            FOR EACH ROW EXECUTE FUNCTION app_private.prevent_soft_delete_resurrection();
    """)

    # Prevent audit modification
    op.execute("""
        CREATE TRIGGER trg_prevent_audit_update
            BEFORE UPDATE OR DELETE ON public.branch_contacts_audit
            FOR EACH ROW EXECUTE FUNCTION app_private.prevent_audit_modification();
    """)

    # Update timestamp (optimized: only on relevant column changes)
    op.execute("""
        CREATE TRIGGER trg_branch_contacts_updated_at
            BEFORE UPDATE OF
                phone_e164, email_normalized, email_raw, is_primary, is_active,
                deleted_at, visibility_scope, channel_capabilities, contact_label, display_format
            ON public.branch_contacts
            FOR EACH ROW EXECUTE FUNCTION app_private.update_timestamp();
    """)

    # Audit trigger (optimized: only on relevant column changes)
    op.execute("""
        CREATE TRIGGER trg_audit_branch_contacts
            AFTER INSERT OR UPDATE OF
                phone_e164, email_normalized, email_raw, is_primary, is_active,
                deleted_at, visibility_scope, channel_capabilities, contact_label, display_format
            OR DELETE ON public.branch_contacts
            FOR EACH ROW EXECUTE FUNCTION app_private.log_branch_contact_changes();
    """)

    # Statement-level invariant handlers (optimized: only on relevant column changes)
    op.execute("""
        CREATE TRIGGER trg_ensure_primary_contact_insert
            AFTER INSERT ON public.branch_contacts
            REFERENCING NEW TABLE AS newly_inserted
            FOR EACH STATEMENT EXECUTE FUNCTION app_private.ensure_primary_contact_insert();
    """)

    op.execute("""
        CREATE TRIGGER trg_ensure_primary_contact_update
            AFTER UPDATE ON public.branch_contacts
            REFERENCING OLD TABLE AS previously_updated NEW TABLE AS newly_updated
            FOR EACH STATEMENT EXECUTE FUNCTION app_private.ensure_primary_contact_update();
    """)

    op.execute("""
        CREATE TRIGGER trg_ensure_primary_contact_delete
            AFTER DELETE ON public.branch_contacts
            REFERENCING OLD TABLE AS previously_deleted
            FOR EACH STATEMENT EXECUTE FUNCTION app_private.ensure_primary_contact_delete();
    """)

    # ===========================================================================
    # SECTION 9: Indices - Zero-Downtime Safe CONCURRENT Creation
    # Base-table CREATE INDEX CONCURRENTLY runs in autocommit blocks.
    # Partitioned audit-table parent indexes are non-concurrent and created while empty.
    # ===========================================================================

    # Standard lookup indices
    index_statements = [
        """
        CREATE INDEX CONCURRENTLY ix_contacts_org_branch_active
        ON public.branch_contacts (org_id, branch_id)
        WHERE (deleted_at IS NULL AND is_active = TRUE);
        """,
        """
        CREATE INDEX CONCURRENTLY ix_active_branch_contacts
        ON public.branch_contacts (branch_id)
        WHERE (deleted_at IS NULL AND is_active = TRUE);
        """,
        """
        CREATE INDEX CONCURRENTLY ix_public_contacts
        ON public.branch_contacts (org_id, visibility_scope)
        WHERE (deleted_at IS NULL AND visibility_scope = 'public');
        """,
        """
        CREATE INDEX CONCURRENTLY ix_primary_contact_lookup
        ON public.branch_contacts(branch_id, contact_kind)
        WHERE (is_primary = TRUE AND is_active = TRUE AND deleted_at IS NULL);
        """,
        """
        CREATE INDEX CONCURRENTLY ix_contacts_search_phone
        ON public.branch_contacts (normalized_digits)
        WHERE (deleted_at IS NULL AND is_active = TRUE);
        """,
        """
        CREATE INDEX CONCURRENTLY ix_contacts_search_email
        ON public.branch_contacts (email_normalized)
        WHERE (deleted_at IS NULL AND is_active = TRUE);
        """,
        # IMPROVEMENT #6: Covering indexes for ordered reads
        """
        CREATE INDEX CONCURRENTLY ix_branch_contacts_primary_ordered
        ON public.branch_contacts (
            branch_id,
            contact_kind,
            is_primary DESC,
            created_at ASC
        )
        INCLUDE (id, phone_e164, email_normalized, visibility_scope)
        WHERE deleted_at IS NULL AND is_active = TRUE;
        """,
        # Unique constraints (split by contact kind to handle NULLs)
        """
        CREATE UNIQUE INDEX CONCURRENTLY uq_public_primary_phone
        ON public.branch_contacts(org_id, phone_e164)
        WHERE (contact_kind = 'phone' AND is_primary = TRUE
            AND visibility_scope = 'public' AND is_active = TRUE AND deleted_at IS NULL);
        """,
        """
        CREATE UNIQUE INDEX CONCURRENTLY uq_public_primary_email
        ON public.branch_contacts(org_id, email_normalized)
        WHERE (contact_kind = 'email' AND is_primary = TRUE
            AND visibility_scope = 'public' AND is_active = TRUE AND deleted_at IS NULL);
        """,
        """
        CREATE UNIQUE INDEX CONCURRENTLY uq_primary_contact_guard_idx
        ON public.branch_contacts (org_id, primary_guard, contact_kind);
        """,
        # Audit indices
        """
        CREATE INDEX ix_audit_contact
        ON public.branch_contacts_audit (branch_contact_id);
        """,
        """
        CREATE INDEX ix_audit_branch_contacts_ordered
        ON public.branch_contacts_audit (
            branch_contact_id,
            changed_at DESC
        );
        """,
        """
        CREATE INDEX ix_audit_org_changed
        ON public.branch_contacts_audit (
            org_id,
            changed_at DESC
        );
        """,
    ]

    for idx_stmt in index_statements:
        with op.get_context().autocommit_block():
            op.execute(idx_stmt)

    # ===========================================================================
    # SECTION 10: Partition Automation Setup
    # ===========================================================================

    # Partition metadata tracking table
    op.execute("""
        CREATE TABLE app_private.partition_metadata (
            table_name VARCHAR(255) NOT NULL,
            partition_name VARCHAR(255) NOT NULL,
            month_start TIMESTAMPTZ NOT NULL,
            month_end TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (table_name, partition_name)
        );
    """)

    # Partition creation function
    op.execute("""
        CREATE FUNCTION app_private.create_branch_contacts_audit_partition(
            partition_month DATE
        )
        RETURNS VOID
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $$
        DECLARE
            partition_name TEXT := format('branch_contacts_audit_%s', to_char(partition_month, 'YYYY_MM'));
            start_date TIMESTAMPTZ := date_trunc('month', partition_month::timestamptz);
            end_date TIMESTAMPTZ := start_date + INTERVAL '1 month';
        BEGIN
            -- Create partition
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.branch_contacts_audit
                 FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date
            );

            -- Local indexes for partition-local scans
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS %I ON public.%I (branch_contact_id, changed_at DESC)',
                partition_name || '_contact_ordered', partition_name
            );

            -- Recent partitions only (optimization)
            IF partition_month > CURRENT_DATE - INTERVAL '6 months' THEN
                EXECUTE format(
                    'CREATE INDEX IF NOT EXISTS %I ON public.%I (changed_by)',
                    partition_name || '_changed_by', partition_name
                );
            END IF;

            -- Compression
            EXECUTE format('ALTER TABLE public.%I ALTER COLUMN changed_fields SET COMPRESSION lz4', partition_name);

            -- Autovacuum tuning
            EXECUTE format(
                'ALTER TABLE public.%I SET (
                    fillfactor = 100,
                    autovacuum_vacuum_scale_factor = 0.02,
                    autovacuum_analyze_scale_factor = 0.01
                 )', partition_name
            );

            -- RLS enforcement
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', partition_name);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', partition_name);

            -- Permissions
            EXECUTE format('GRANT SELECT, INSERT ON public.%I TO app_user', partition_name);
            EXECUTE format('ALTER TABLE public.%I OWNER TO app_rls_executor', partition_name);

            -- Metadata tracking
            INSERT INTO app_private.partition_metadata
                (table_name, partition_name, month_start, month_end)
            VALUES ('branch_contacts_audit', partition_name, start_date, end_date)
            ON CONFLICT DO NOTHING;

            RAISE LOG 'Created audit partition: %', partition_name;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.create_branch_contacts_audit_partition(DATE) FROM PUBLIC;")

    # Create initial partitions (current month + next 11 months)
    for i in range(12):
        with op.get_context().autocommit_block():
            op.execute(f"""
                SELECT app_private.create_branch_contacts_audit_partition(
                    (CURRENT_DATE + INTERVAL '{i} months')::DATE
                );
            """)

    # ===========================================================================
    # SECTION 11: Final Ownership Transfer
    # All privilege, RLS, policy, constraint, index, trigger, and partition
    # hardening is complete before externally managed ownership is applied.
    # app_rls_executor receives CREATE on app_private only while accepting
    # private-object ownership; the grant is revoked before the upgrade exits.
    # ===========================================================================
    op.execute("ALTER TABLE public.branch_contacts OWNER TO app_rls_executor;")
    op.execute("ALTER TABLE public.branch_contacts_audit OWNER TO app_rls_executor;")
    op.execute("ALTER TABLE public.branch_contacts_audit_default OWNER TO app_rls_executor;")
    _rb1l7_prepare_temporary_app_private_create()
    op.execute("GRANT CREATE ON SCHEMA app_private TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.prevent_soft_delete_resurrection() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.prevent_audit_modification() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.update_timestamp() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.log_branch_contact_changes() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.process_primary_contact_batch(UUID[]) OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.ensure_primary_contact_insert() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.ensure_primary_contact_update() OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.ensure_primary_contact_delete() OWNER TO app_rls_executor;")
    op.execute("ALTER TABLE app_private.partition_metadata OWNER TO app_rls_executor;")
    op.execute("ALTER FUNCTION app_private.create_branch_contacts_audit_partition(DATE) OWNER TO app_rls_executor;")
    op.execute("REVOKE CREATE ON SCHEMA app_private FROM app_rls_executor;")
    _rb1l7_restore_temporary_app_private_create()
    _rb1l7_finalize_0020_markers()


def downgrade():
    """Remove 0020 only when predecessor 00f can be restored losslessly."""
    state = _rb1l7_load_and_validate_0020_state()
    _0020_preflight_downgrade_domain()

    # Domain infrastructure is owned by app_rls_executor. SET LOCAL keeps the
    # ownership switch transaction-scoped and is restored before ACL markers.
    op.execute("SET LOCAL ROLE app_rls_executor;")

    # Remove explicit dependents before their functions/tables. Exact names are
    # intentional: catalog drift must fail rather than be silently accepted.
    op.execute("DROP TRIGGER trg_prevent_soft_delete_resurrection ON public.branch_contacts;")
    op.execute("DROP TRIGGER trg_prevent_audit_update ON public.branch_contacts_audit;")
    op.execute("DROP TRIGGER trg_branch_contacts_updated_at ON public.branch_contacts;")
    op.execute("DROP TRIGGGER trg_audit_branch_contacts ON public.branch_contacts;")
    op.execute("DROP TRIGGER trg_ensure_primary_contact_insert ON public.branch_contacts;")
    op.execute("DROP TRIGGGER trg_ensure_primary_contact_update ON public.branch_contacts;")
    op.execute("DROP TRIGGGER trg_ensure_primary_contact_delete ON public.branch_contacts;")

    op.execute("DROP POLICY tenant_isolation_contacts_audit ON public.branch_contacts_audit;")
    op.execute("DROP POLICY tenant_isolation_contacts ON public.branch_contacts;")

    op.execute("DROP FUNCTION app_private.ensure_primary_contact_insert() RESTRICT;")
    op.execute("DROP FUNCTION app_private.ensure_primary_contact_update() RESTRICT;")
    op.execute("DROP FUNCTION app_private.ensure_primary_contact_delete() RESTRICT;")
    op.execute("DROP FUNCTION app_private.process_primary_contact_batch(UUID[]) RESTRICT;")
    op.execute("DROP FUNCTION app_private.log_branch_contact_changes() RESTRICT;")
    op.execute("DROP FUNCTION app_private.update_timestamp() RESTRICT;")
    op.execute("DROP FUNCTION app_private.prevent_audit_modification() RESTRICT;")
    op.execute("DROP FUNCTION app_private.prevent_soft_delete_resurrection() RESTRICT;")
    op.execute("DROP FUNCTION app_private.create_branch_contacts_audit_partition(DATE) RESTRICT;")

    # Partition children and table-owned indexes are internal dependencies of
    # the parents; RESTRICT still blocks unrelated external dependents.
    op.execute("DROP TABLE public.branch_contacts_audit RESTRICT;")
    op.execute("DROP TABLE public.branch_contacts RESTRICT;")
    op.execute("DROP TABLE app_private.partition_metadata RESTRICT;")

    op.execute("RESET ROLE;")
    _rb1l7_restore_0020_acl_state(state)
    _rb1l7_drop_0020_markers_and_maybe_schema(state)

    # Types are revision-owned by migration_owner. RESTRICT proves no external
    # object still depends on them.
    op.execute("DROP TYPE public.contact_kind_enum RESTRICT;")
    op.execute("DROP TYPE public.visibility_scope_enum RESTRICT;")
    op.execute("DROP TYPE public.audit_action_enum RESTRICT;")
    op.execute("DROP TYPE public.verification_method_enum RESTRICT;")
