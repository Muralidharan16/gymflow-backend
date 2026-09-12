"""RBAC Hardening Phase 7 — Immutable Role-Permission Ledger

Phase 7 of the v18.0 hardening plan.

Creates:
  • public.role_permission_events
      — Immutable append-only ledger for role->permission mappings.
      — event_type: 'grant' or 'revoke'.
      — No updates or deletes allowed (enforced via grants + trigger).

  • public.effective_role_permissions
      — Materialized projection of the ledger (the active permission cache).
      — Used by Phase 6 compile_member_permissions() for fast joins.
      — Includes drift metadata (projected_at, ledger_watermark).

  • app_private.rebuild_effective_role_permissions()
      — Replays the event ledger to rebuild the projection atomically.

  • app_private.raise_ledger_immutable_violation()
      — BEFORE UPDATE/DELETE trigger to protect role_permission_events.

Modifies:
  • app_private.compile_member_permissions()
      — Drops the hardcoded CASE WHEN mapping.
      — Joins against the new effective_role_permissions projection.

Revision ID: 0028_rbac_p7_role_events
Revises: 0027_rbac_p6_perm_snapshots
Create Date: 2026-05-23
"""

from alembic import op
import sqlalchemy as sa
import hashlib
import json

revision = "0028_rbac_p7_role_events"
down_revision = "0027_rbac_p6_perm_snapshots"
branch_labels = None
depends_on = None


# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START
from types import MappingProxyType

_RB1L7_MARKER_VERSION = 1
_RB1L7_REVISION = '0028_rbac_p7_role_events'
_RB1L7_ACL_MARKER = 'app_private.migration_0028_schema_acl_state'
_RB1L7_ACL_OPERATIONS = (('GRANT', 'public', 'app_security_owner', 'USAGE'),)


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



# RB1M2W_0028_COMPLETE_OWNER_CONTEXT_HELPERS_START

_RB1M2W_MIGRATION_OWNER = "migration_owner"
_RB1M2W_SECURITY_OWNER = "app_security_owner"
_RB1M2W_PRIVATE_SCHEMA = "app_private"
_RB1M2W_PUBLIC_SCHEMA = "public"
_RB1M2W_OWNED_FUNCTIONS = (
    "app_private.raise_ledger_immutable_violation()",
    "app_private.rebuild_effective_role_permissions()",
    "app_private.trigger_rebuild_role_permissions()",
)
_RB1M2W_COMPILE_FUNCTION = (
    "app_private.compile_member_permissions("
    "uuid,uuid,uuid,smallint)"
)
_RB1M2W_OWNED_RELATION = "public.effective_role_permissions"


def _rb1m2w_identity(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()


def _rb1m2w_require_migration_owner(bind):
    identity = _rb1m2w_identity(bind)
    if (
        identity["session_user_name"] != _RB1M2W_MIGRATION_OWNER
        or identity["current_user_name"] != _RB1M2W_MIGRATION_OWNER
    ):
        raise RuntimeError(
            "Revision-0028 requires both session_user and current_user "
            "to be migration_owner: "
            f"{dict(identity)!r}."
        )


def _rb1m2w_can_set_security_owner(bind):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_has_role(
                    :session_role,
                    :target_role,
                    'SET'
                )
                """
            ),
            {
                "session_role": _RB1M2W_MIGRATION_OWNER,
                "target_role": _RB1M2W_SECURITY_OWNER,
            },
        ).scalar_one()
    )


def _rb1m2w_schema_row(bind, schema_name):
    return bind.execute(
        sa.text(
            """
            SELECT
                namespace.oid AS schema_oid,
                pg_catalog.pg_get_userbyid(
                    namespace.nspowner
                )::text AS owner_name
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = :schema_name
            """
        ),
        {"schema_name": schema_name},
    ).mappings().one_or_none()


def _rb1m2w_public_has_schema_create(bind, schema_name):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_namespace AS namespace
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        namespace.nspacl
                    ) AS acl
                    WHERE namespace.nspname = :schema_name
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'CREATE'
                )
                """
            ),
            {"schema_name": schema_name},
        ).scalar_one()
    )


def _rb1m2w_direct_schema_acl_rows(
    bind,
    schema_name,
    grantee_name,
):
    return tuple(
        (
            row["grantor_name"],
            row["grantee_name"],
            row["privilege_type"],
            bool(row["is_grantable"]),
        )
        for row in bind.execute(
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
                  AND grantee_role.rolname = :grantee_name
                ORDER BY
                    grantor_role.rolname,
                    acl.privilege_type,
                    acl.is_grantable
                """
            ),
            {
                "schema_name": schema_name,
                "grantee_name": grantee_name,
            },
        ).mappings()
    )


def _rb1m2w_has_schema_privilege(
    bind,
    role_name,
    schema_name,
    privilege,
):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_schema_privilege(
                    role_data.oid,
                    namespace.oid,
                    :privilege
                )
                FROM pg_catalog.pg_roles AS role_data
                CROSS JOIN pg_catalog.pg_namespace AS namespace
                WHERE role_data.rolname = :role_name
                  AND namespace.nspname = :schema_name
                """
            ),
            {
                "role_name": role_name,
                "schema_name": schema_name,
                "privilege": privilege,
            },
        ).scalar_one()
    )


def _rb1m2w_function_row(bind, signature):
    return bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(
                    function_data.proowner
                )::text AS owner_name,
                function_data.prosecdef AS security_definer,
                function_data.proconfig
            FROM pg_catalog.pg_proc AS function_data
            WHERE function_data.oid =
                pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().one_or_none()


def _rb1m2w_relation_row(bind, schema_name, relation_name):
    return bind.execute(
        sa.text(
            """
            SELECT
                relation.relkind::text AS relation_kind,
                pg_catalog.pg_get_userbyid(
                    relation.relowner
                )::text AS owner_name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = :schema_name
              AND relation.relname = :relation_name
            """
        ),
        {
            "schema_name": schema_name,
            "relation_name": relation_name,
        },
    ).mappings().one_or_none()


def _rb1m2w_preflight_upgrade(bind):
    _rb1m2w_require_migration_owner(bind)

    if not _rb1m2w_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )

    private_schema = _rb1m2w_schema_row(
        bind,
        _RB1M2W_PRIVATE_SCHEMA,
    )
    public_schema = _rb1m2w_schema_row(
        bind,
        _RB1M2W_PUBLIC_SCHEMA,
    )
    if private_schema is None or public_schema is None:
        raise RuntimeError(
            "Revision-0028 required schemas are absent."
        )
    if private_schema["owner_name"] != _RB1M2W_MIGRATION_OWNER:
        raise RuntimeError(
            "app_private owner drift: "
            f"{dict(private_schema)!r}."
        )

    for schema_name in (
        _RB1M2W_PRIVATE_SCHEMA,
        _RB1M2W_PUBLIC_SCHEMA,
    ):
        if _rb1m2w_public_has_schema_create(bind, schema_name):
            raise RuntimeError(
                f"PUBLIC CREATE on {schema_name} is forbidden."
            )

    for privilege in ("USAGE", "CREATE"):
        if not _rb1m2w_has_schema_privilege(
            bind,
            _RB1M2W_MIGRATION_OWNER,
            _RB1M2W_PRIVATE_SCHEMA,
            privilege,
        ):
            raise RuntimeError(
                "migration_owner lacks "
                f"{privilege} on app_private."
            )

    if not _rb1m2w_has_schema_privilege(
        bind,
        _RB1M2W_SECURITY_OWNER,
        _RB1M2W_PRIVATE_SCHEMA,
        "USAGE",
    ):
        raise RuntimeError(
            "app_security_owner lacks USAGE on app_private."
        )

    for schema_name in (
        _RB1M2W_PRIVATE_SCHEMA,
        _RB1M2W_PUBLIC_SCHEMA,
    ):
        if not _rb1m2w_has_schema_privilege(
            bind,
            _RB1M2W_SECURITY_OWNER,
            schema_name,
            "CREATE",
        ) and not _rb1m2w_has_schema_privilege(
            bind,
            _RB1M2W_MIGRATION_OWNER,
            schema_name,
            "CREATE WITH GRANT OPTION",
        ):
            raise RuntimeError(
                "migration_owner cannot open the required bounded "
                f"CREATE window on {schema_name}."
            )

    for signature in _RB1M2W_OWNED_FUNCTIONS:
        if _rb1m2w_function_row(bind, signature) is not None:
            raise RuntimeError(
                "Revision-0028 target function already exists: "
                f"{signature}."
            )

    compile_function = _rb1m2w_function_row(
        bind,
        _RB1M2W_COMPILE_FUNCTION,
    )
    if (
        compile_function is None
        or compile_function["owner_name"]
        != _RB1M2W_SECURITY_OWNER
    ):
        raise RuntimeError(
            "Revision-0028 predecessor compile function owner drift: "
            f"{compile_function!r}."
        )

    for relation_name in (
        "role_permission_events",
        "effective_role_permissions",
    ):
        if _rb1m2w_relation_row(
            bind,
            "public",
            relation_name,
        ) is not None:
            raise RuntimeError(
                "Revision-0028 target relation already exists: "
                f"public.{relation_name}."
            )


def _rb1m2w_prepare_owner_context(bind):
    _rb1m2w_require_migration_owner(bind)
    if not _rb1m2w_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )

    # Ephemeral CREATE-only journal. Durable ACL operations, including
    # RB1L7's public-schema USAGE grant, are intentionally outside this
    # state and must not be mistaken for temporary owner-transfer leakage.
    before_create = {
        schema_name: tuple(
            row
            for row in sorted(
                _rb1m2w_direct_schema_acl_rows(
                    bind,
                    schema_name,
                    _RB1M2W_SECURITY_OWNER,
                )
            )
            if row[2] == "CREATE"
        )
        for schema_name in (
            _RB1M2W_PRIVATE_SCHEMA,
            _RB1M2W_PUBLIC_SCHEMA,
        )
    }
    added_create = []

    for schema_name in (
        _RB1M2W_PRIVATE_SCHEMA,
        _RB1M2W_PUBLIC_SCHEMA,
    ):
        if not before_create[schema_name]:
            bind.execute(
                sa.text(
                    f"GRANT CREATE ON SCHEMA {schema_name} "
                    "TO app_security_owner"
                )
            )
            added_create.append(schema_name)

        if not _rb1m2w_has_schema_privilege(
            bind,
            _RB1M2W_SECURITY_OWNER,
            schema_name,
            "CREATE",
        ):
            raise RuntimeError(
                "Temporary owner-transfer CREATE grant failed on "
                f"{schema_name}."
            )

    return {
        "before_create": before_create,
        "added_create": tuple(added_create),
    }


def _rb1m2w_restore_owner_context(bind, state):
    _rb1m2w_require_migration_owner(bind)

    for schema_name in reversed(state["added_create"]):
        bind.execute(
            sa.text(
                f"REVOKE CREATE ON SCHEMA {schema_name} "
                "FROM app_security_owner"
            )
        )

    for schema_name, expected in state["before_create"].items():
        observed = tuple(
            row
            for row in sorted(
                _rb1m2w_direct_schema_acl_rows(
                    bind,
                    schema_name,
                    _RB1M2W_SECURITY_OWNER,
                )
            )
            if row[2] == "CREATE"
        )
        if observed != expected:
            raise RuntimeError(
                "Revision-0028 schema ACL restoration failed: temporary CREATE ACL restoration mismatch for "
                f"{schema_name}: observed={observed!r}, "
                f"expected={expected!r}."
            )


def _rb1m2w_execute_as_security_owner(bind, sql):
    _rb1m2w_require_migration_owner(bind)
    if not _rb1m2w_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    identity = _rb1m2w_identity(bind)
    if identity["session_user_name"] != _RB1M2W_MIGRATION_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE changed session_user unexpectedly."
        )
    if identity["current_user_name"] != _RB1M2W_SECURITY_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE did not enter app_security_owner."
        )

    bind.execute(sa.text(sql))
    bind.execute(sa.text("RESET ROLE"))
    _rb1m2w_require_migration_owner(bind)


def _rb1m2w_preflight_downgrade(bind):
    _rb1m2w_require_migration_owner(bind)
    if not _rb1m2w_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )

    for signature in _RB1M2W_OWNED_FUNCTIONS:
        row = _rb1m2w_function_row(bind, signature)
        if (
            row is None
            or row["owner_name"] != _RB1M2W_SECURITY_OWNER
        ):
            raise RuntimeError(
                "Revision-0028 owned-function downgrade drift for "
                f"{signature}: {row!r}."
            )

    compile_function = _rb1m2w_function_row(
        bind,
        _RB1M2W_COMPILE_FUNCTION,
    )
    if (
        compile_function is None
        or compile_function["owner_name"]
        != _RB1M2W_SECURITY_OWNER
    ):
        raise RuntimeError(
            "Revision-0028 compile-function downgrade owner drift: "
            f"{compile_function!r}."
        )

    relation = _rb1m2w_relation_row(
        bind,
        "public",
        "effective_role_permissions",
    )
    if (
        relation is None
        or relation["owner_name"] != _RB1M2W_SECURITY_OWNER
        or relation["relation_kind"] not in {"r", "p"}
    ):
        raise RuntimeError(
            "Revision-0028 projection owner/kind downgrade drift: "
            f"{relation!r}."
        )


def _rb1m2w_drop_owned_function(bind, signature):
    if signature not in _RB1M2W_OWNED_FUNCTIONS:
        raise RuntimeError(
            f"Unapproved revision-0028 function: {signature!r}."
        )
    _rb1m2w_execute_as_security_owner(
        bind,
        f"DROP FUNCTION {signature} RESTRICT",
    )


def _rb1m2w_drop_owned_relation(bind, relation_name):
    if relation_name != _RB1M2W_OWNED_RELATION:
        raise RuntimeError(
            f"Unapproved revision-0028 relation: {relation_name!r}."
        )
    _rb1m2w_execute_as_security_owner(
        bind,
        "DROP TABLE public.effective_role_permissions RESTRICT",
    )


# RB1M2W_0028_COMPLETE_OWNER_CONTEXT_HELPERS_END


def upgrade() -> None:

    # ── 1. Immutable Event Ledger ─────────────────────────────────────────
    bind = _rb1l7_bind()
    _rb1m2w_preflight_upgrade(bind)
    owner_state = _rb1m2w_prepare_owner_context(bind)

    op.execute("""
        CREATE TABLE public.role_permission_events (
            id            BIGSERIAL   PRIMARY KEY,
            role_id       SMALLINT    NOT NULL
                          REFERENCES public.staff_roles(id) ON DELETE RESTRICT,
            permission_id SMALLINT    NOT NULL
                          REFERENCES public.permissions(id) ON DELETE RESTRICT,
            event_type    VARCHAR(16) NOT NULL
                          CHECK (event_type IN ('grant','revoke')),
            performed_by  UUID        NULL
                          REFERENCES public.organization_users(id) ON DELETE RESTRICT,
            reason_code   VARCHAR(32) NOT NULL DEFAULT 'system.bootstrap',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.role_permission_events IS
            'Immutable append-only ledger for role->permission mappings. '
            'Source of truth for what permissions a role holds at any point in time.';
    """)

    # ── 2. Ledger Immutability Guard ──────────────────────────────────────
    op.execute("REVOKE UPDATE, DELETE ON public.role_permission_events FROM app_runtime;")
    op.execute("GRANT INSERT, SELECT ON public.role_permission_events TO app_runtime;")
    op.execute("GRANT SELECT ON public.role_permission_events TO audit_writer, readonly_analytics;")
    op.execute("GRANT ALL ON public.role_permission_events TO app_security_owner;")
    op.execute("GRANT ALL ON SEQUENCE public.role_permission_events_id_seq TO app_security_owner;")

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.raise_ledger_immutable_violation()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'Security policy violation: Role permission events are immutable. '
                'To change a role, append a new grant/revoke event.'
            USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.raise_ledger_immutable_violation() FROM PUBLIC;")

    op.execute("""
            CREATE TRIGGER trg_deny_role_event_mutation
                BEFORE UPDATE OR DELETE ON public.role_permission_events
                FOR EACH ROW
                EXECUTE FUNCTION app_private.raise_ledger_immutable_violation();
        """)

    op.execute("ALTER FUNCTION app_private.raise_ledger_immutable_violation() OWNER TO app_security_owner;")

    # ── 3. Projected Cache (Materialized State) ───────────────────────────
    op.execute("""
        CREATE TABLE public.effective_role_permissions (
            role_id           SMALLINT NOT NULL
                              REFERENCES public.staff_roles(id) ON DELETE CASCADE,
            permission_id     SMALLINT NOT NULL
                              REFERENCES public.permissions(id) ON DELETE CASCADE,

            -- Projection metadata for drift detection
            projected_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            projector_version INT NOT NULL DEFAULT 1,
            ledger_watermark  BIGINT NOT NULL,

            PRIMARY KEY (role_id, permission_id)
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.effective_role_permissions IS
            'Materialized projection of role_permission_events. '
            'Read-heavy cache used by RLS and token generation. '
            'Rebuilt automatically when events are appended.';
    """)

    op.execute("ALTER TABLE public.effective_role_permissions OWNER TO app_security_owner;")
    _rb1l7_prepare_revision_schema_acl_state()
    _rb1m2w_execute_as_security_owner(bind, "GRANT SELECT ON public.effective_role_permissions TO app_runtime, readonly_analytics;")

    # ── 4. Ledger Replay Function ─────────────────────────────────────────
    # Atomically drops and re-projects the active permissions based on the ledger.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.rebuild_effective_role_permissions()
        RETURNS VOID
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_watermark BIGINT;
        BEGIN
            -- 1. Lock the ledger to ensure a consistent snapshot
            LOCK TABLE public.role_permission_events IN ACCESS SHARE MODE;

            -- 2. Grab the current high-water mark
            SELECT COALESCE(MAX(id), 0) INTO v_watermark
            FROM public.role_permission_events;

            -- 3. Delete old projection completely
            DELETE FROM public.effective_role_permissions;

            -- 4. Replay events in order, taking the LAST event per role+perm
            --    as the final truth (grant or revoke).
            INSERT INTO public.effective_role_permissions (
                role_id,
                permission_id,
                ledger_watermark
            )
            SELECT
                role_id,
                permission_id,
                v_watermark
            FROM (
                SELECT
                    role_id,
                    permission_id,
                    event_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY role_id, permission_id
                        ORDER BY id DESC
                    ) as rn
                FROM public.role_permission_events
                WHERE id <= v_watermark
            ) latest_events
            WHERE latest_events.rn = 1
              AND latest_events.event_type = 'grant';

        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.rebuild_effective_role_permissions() FROM PUBLIC;")
    op.execute("ALTER FUNCTION app_private.rebuild_effective_role_permissions() OWNER TO app_security_owner;")

    # ── 5. Auto-Rebuild Trigger ───────────────────────────────────────────
    # Whenever a new event is appended, rebuild the cache.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.trigger_rebuild_role_permissions()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM app_private.rebuild_effective_role_permissions();
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("REVOKE ALL ON FUNCTION app_private.trigger_rebuild_role_permissions() FROM PUBLIC;")

    op.execute("""
            CREATE TRIGGER trg_auto_rebuild_role_perms
                AFTER INSERT ON public.role_permission_events
                FOR EACH STATEMENT
                EXECUTE FUNCTION app_private.trigger_rebuild_role_permissions();
        """)

    op.execute("ALTER FUNCTION app_private.trigger_rebuild_role_permissions() OWNER TO app_security_owner;")

    # ── 6. Update Phase 6 compile_member_permissions() ────────────────────
    # Remove the hardcoded CASE logic; use the new effective_role_permissions
    # projection instead.
    _rb1m2w_execute_as_security_owner(bind, """
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
                -- Joins branch_staff_roles → effective_role_permissions → permissions
                SELECT jsonb_agg(DISTINCT p.code ORDER BY p.code)
                INTO   v_permission_codes
                FROM   public.branch_staff_roles bsr
                JOIN   public.effective_role_permissions erp ON erp.role_id = bsr.role_id
                JOIN   public.permissions p                  ON p.id = erp.permission_id
                WHERE  bsr.organization_member_id = p_organization_member_id
                  AND  bsr.org_id                 = p_org_id
                  AND  bsr.branch_id              = p_branch_id
                  AND  bsr.scope_type_id          = p_scope_type_id
                  AND  bsr.revoked_at             IS NULL
                  AND  bsr.deleted_at             IS NULL
                  AND  bsr.effective_from         <= clock_timestamp()
                  AND (bsr.effective_to           IS NULL OR bsr.effective_to > clock_timestamp());

                RETURN COALESCE(v_permission_codes, '[]'::jsonb);
            END;
            $$;
        """)

    _rb1m2w_restore_owner_context(bind, owner_state)

    # ── 7. Seed Initial System Role Permissions ───────────────────────────
    # Since we are removing the hardcoded logic, we must seed the ledger
    # so that the system roles (owner, admin, etc.) still work.

    # 1='owner', 2='admin', 3='manager', 4='trainer', 5='receptionist', 6='auditor'
    op.execute("""
        WITH role_map AS (
            SELECT 1 as r_id, p.id as p_id FROM public.permissions p -- Owner: all
            UNION ALL
            SELECT 2 as r_id, p.id as p_id FROM public.permissions p WHERE p.code != 'org.settings.update' -- Admin: all except org.settings.update
            UNION ALL
            SELECT 3 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN (
                'branch.read','branch.update','branch.suspend',
                'staff_roles.read','staff_roles.assign','staff_roles.revoke',
                'members.read','members.invite','members.suspend'
            )
            UNION ALL
            SELECT 4 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('branch.read','members.read')
            UNION ALL
            SELECT 5 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('branch.read','members.read','members.invite')
            UNION ALL
            SELECT 6 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('audit.read','branch.read')
        )
        INSERT INTO public.role_permission_events (role_id, permission_id, event_type, reason_code)
        SELECT r_id, p_id, 'grant', 'system.bootstrap'
        FROM role_map;
    """)
    _rb1l7_finalize_revision_schema_acl_state()

    # We do not need to call rebuild explicitly, the AFTER INSERT trigger did it!


def downgrade() -> None:
    # 1. Restore compile_member_permissions to hardcoded version
    bind = _rb1l7_bind()
    _rb1m2w_preflight_downgrade(bind)
    owner_state = _rb1m2w_prepare_owner_context(bind)
    _rb1m2w_execute_as_security_owner(bind, """CREATE OR REPLACE FUNCTION app_private.compile_member_permissions(p_organization_member_id uuid, p_org_id uuid, p_branch_id uuid, p_scope_type_id smallint DEFAULT 2)
 RETURNS jsonb
 LANGUAGE plpgsql
 STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'off'
AS $function$
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
        $function$
""")

    # 2. Drop triggers and functions
    op.execute("DROP TRIGGER IF EXISTS trg_auto_rebuild_role_perms ON public.role_permission_events;")
    _rb1m2w_drop_owned_function(
        bind, 'app_private.trigger_rebuild_role_permissions()'
    )
    _rb1m2w_drop_owned_function(
        bind, 'app_private.rebuild_effective_role_permissions()'
    )
    op.execute("DROP TRIGGER IF EXISTS trg_deny_role_event_mutation ON public.role_permission_events;")
    _rb1m2w_drop_owned_function(
        bind, 'app_private.raise_ledger_immutable_violation()'
    )

    # 3. Drop tables
    _rb1m2w_drop_owned_relation(
        bind, "public.effective_role_permissions"
    )
    op.execute(
        "DROP TABLE IF EXISTS public.role_permission_events RESTRICT;"
    )
    _rb1m2w_restore_owner_context(bind, owner_state)
    _rb1l7_restore_revision_schema_acl_state()
