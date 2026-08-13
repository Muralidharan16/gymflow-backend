"""Manage auth session partitions with externally provisioned pg_partman.

Revision ID: 3c4d5e6f7081
Revises: 2b3c4d5e6f70
Create Date: 2026-08-09 00:00:00.000000

Phase 9 created ``public.auth_sessions`` as a declarative RANGE-partitioned
relation but provisioned only a single May-2026 child. That historical shape
cannot accept sessions once the hard-coded partition ages out. This revision
adopts the existing partition set into the already-provisioned pg_partman 5.0.1
infrastructure and establishes an explicit monthly maintenance contract.

The pg_partman extension remains infrastructure-owned. Alembic never creates,
drops, or assumes ownership of the extension. No DEFAULT partition is created:
production must execute pg_partman maintenance on schedule and monitor the
future-partition runway. A twelve-month premake window provides operational
headroom without allowing a catch-all partition to hide maintenance failure.
"""

from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3c4d5e6f7081"
down_revision: Union[str, Sequence[str], None] = "2b3c4d5e6f70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MIGRATION_OWNER = "migration_owner"
_PARTMAN_EXTENSION = "pg_partman"
_PARTMAN_SCHEMA = "partman"
_PARTMAN_VERSION = "5.0.1"
_PARENT_SCHEMA = "public"
_PARENT_NAME = "auth_sessions"
_PARENT = f"{_PARENT_SCHEMA}.{_PARENT_NAME}"
_LEGACY_CHILD = "auth_sessions_y2026_m05"
_CANONICAL_LEGACY_CHILD = "auth_sessions_p20260501"
_DEFAULT_RELATION = "public.auth_sessions_default"
_PREMAKE = 12
_START_PARTITION = "2026-05-01 00:00:00+00"
_GENERATED_CHILD_RE = re.compile(r"^auth_sessions_p\d{8}$")


def _fetch_one(bind, sql: str, parameters=None):
    row = bind.execute(sa.text(sql), parameters or {}).mappings().one_or_none()
    return dict(row) if row is not None else None


def _fetch_all(bind, sql: str, parameters=None):
    return [
        dict(row)
        for row in bind.execute(sa.text(sql), parameters or {}).mappings().all()
    ]


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(name: str) -> str:
    return f"{_quote_ident(_PARENT_SCHEMA)}.{_quote_ident(name)}"


def _require_migration_identity(bind) -> None:
    row = _fetch_one(
        bind,
        """
        SELECT
            session_user::text AS session_name,
            current_user::text AS current_name,
            role.rolsuper AS is_superuser,
            role.rolbypassrls AS bypasses_rls,
            role.rolcreatedb AS can_create_database,
            role.rolcreaterole AS can_create_role,
            role.rolreplication AS can_replicate
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """,
    )
    if row is None:
        raise RuntimeError("Unable to resolve the Alembic execution role.")

    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "Auth-session partition adoption requires both session_user and "
            f"current_user to be {_MIGRATION_OWNER}; observed "
            f"session={row['session_name']!r}, current={row['current_name']!r}."
        )

    unsafe = {
        key: row[key]
        for key in (
            "is_superuser",
            "bypasses_rls",
            "can_create_database",
            "can_create_role",
            "can_replicate",
        )
        if row[key]
    }
    if unsafe:
        raise RuntimeError(
            "Auth-session partition adoption refuses an over-privileged "
            f"migration_owner: {unsafe!r}."
        )


def _require_partman_dependency(bind) -> None:
    row = _fetch_one(
        bind,
        """
        SELECT
            extension.extversion::text AS extension_version,
            namespace.nspname::text AS schema_name,
            owner.rolname::text AS owner_name
        FROM pg_catalog.pg_extension AS extension
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = extension.extnamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = extension.extowner
        WHERE extension.extname = :extension_name
        """,
        {"extension_name": _PARTMAN_EXTENSION},
    )
    if row is None:
        raise RuntimeError(
            "Required externally provisioned pg_partman extension is absent. "
            "Install it as database infrastructure before running Alembic."
        )
    if row["schema_name"] != _PARTMAN_SCHEMA:
        raise RuntimeError(
            "pg_partman must be installed in schema partman; observed "
            f"{row['schema_name']!r}."
        )
    if row["extension_version"] != _PARTMAN_VERSION:
        raise RuntimeError(
            "Unsupported pg_partman version for auth-session partition "
            f"adoption: observed={row['extension_version']!r}, "
            f"required={_PARTMAN_VERSION!r}."
        )
    if row["owner_name"] == _MIGRATION_OWNER:
        raise RuntimeError(
            "pg_partman must remain infrastructure-owned; migration_owner "
            "must not own the extension."
        )

    create_parent = _fetch_one(
        bind,
        """
        WITH target AS (
            SELECT pg_catalog.to_regprocedure(:signature) AS routine_oid
        )
        SELECT
            target.routine_oid::oid::bigint AS routine_oid,
            pg_catalog.has_function_privilege(
                current_user,
                target.routine_oid,
                'EXECUTE'
            ) AS can_execute
        FROM target
        WHERE target.routine_oid IS NOT NULL
        """,
        {
            "signature": (
                "partman.create_parent("
                "text,text,text,text,text,integer,text,boolean,text,"
                "text[],text,boolean,text)"
            )
        },
    )
    if create_parent is None or not create_parent["can_execute"]:
        raise RuntimeError(
            "migration_owner lacks EXECUTE on the supported pg_partman "
            "create_parent signature."
        )

    privileges = _fetch_one(
        bind,
        """
        SELECT
            pg_catalog.has_schema_privilege(
                current_user,
                :partman_schema,
                'USAGE'
            ) AS partman_usage,
            pg_catalog.has_schema_privilege(
                current_user,
                :parent_schema,
                'CREATE'
            ) AS parent_schema_create,
            pg_catalog.has_table_privilege(
                current_user,
                'partman.part_config',
                'SELECT'
            ) AS config_select,
            pg_catalog.has_table_privilege(
                current_user,
                'partman.part_config',
                'INSERT'
            ) AS config_insert,
            pg_catalog.has_table_privilege(
                current_user,
                'partman.part_config',
                'UPDATE'
            ) AS config_update,
            pg_catalog.has_table_privilege(
                current_user,
                'partman.part_config',
                'DELETE'
            ) AS config_delete
        """,
        {
            "partman_schema": _PARTMAN_SCHEMA,
            "parent_schema": _PARENT_SCHEMA,
        },
    )
    if privileges is None or not all(privileges.values()):
        raise RuntimeError(
            "migration_owner lacks the bounded privileges required to manage "
            f"auth-session partitions with pg_partman: {privileges!r}."
        )


def _partition_rows(bind):
    return _fetch_all(
        bind,
        """
        SELECT
            child.relname::text AS child_name,
            owner.rolname::text AS owner_name,
            pg_catalog.pg_get_expr(
                child.relpartbound,
                child.oid,
                true
            )::text AS partition_bound
        FROM pg_catalog.pg_inherits AS inheritance
        JOIN pg_catalog.pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_catalog.pg_namespace AS parent_namespace
          ON parent_namespace.oid = parent.relnamespace
        JOIN pg_catalog.pg_class AS child
          ON child.oid = inheritance.inhrelid
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = child.relowner
        WHERE parent_namespace.nspname = :schema_name
          AND parent.relname = :relation_name
        ORDER BY child.relname
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "relation_name": _PARENT_NAME,
        },
    )


def _lock_and_require_predecessor(bind) -> None:
    bind.execute(sa.text(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE"))

    parent = _fetch_one(
        bind,
        """
        SELECT
            relation.relkind::text AS relation_kind,
            owner.rolname::text AS owner_name,
            pg_catalog.pg_get_partkeydef(relation.oid)::text AS partition_key
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = relation.relowner
        WHERE namespace.nspname = :schema_name
          AND relation.relname = :relation_name
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "relation_name": _PARENT_NAME,
        },
    )
    expected_parent = {
        "relation_kind": "p",
        "owner_name": _MIGRATION_OWNER,
        "partition_key": "RANGE (created_at)",
    }
    if parent != expected_parent:
        raise RuntimeError(
            "Auth-session predecessor parent contract drifted: "
            f"observed={parent!r}, expected={expected_parent!r}."
        )

    rows = _partition_rows(bind)
    if len(rows) != 1 or rows[0]["child_name"] != _LEGACY_CHILD:
        raise RuntimeError(
            "Auth-session predecessor partition membership drifted. "
            "Expected exactly the historical May-2026 child and no manual "
            f"partition drift; observed={rows!r}."
        )

    row = rows[0]
    if row["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "Historical auth-session partition ownership drifted: "
            f"{row!r}."
        )

    bound = row["partition_bound"] or ""
    if "2026-05-01" not in bound or "2026-06-01" not in bound:
        raise RuntimeError(
            "Historical auth-session May-2026 partition bound drifted: "
            f"{bound!r}."
        )

    conflict = _fetch_one(
        bind,
        """
        SELECT relation.relname::text AS relation_name
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = :schema_name
          AND relation.relname = :canonical_name
        LIMIT 1
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "canonical_name": _CANONICAL_LEGACY_CHILD,
        },
    )
    if conflict is not None:
        raise RuntimeError(
            "Cannot safely adopt auth-session partitions because a target "
            f"relation already exists: {conflict!r}."
        )


def _rename_legacy_child(bind, old_name: str, new_name: str) -> None:
    bind.execute(
        sa.text(
            "ALTER TABLE "
            f"{_qualified(old_name)} "
            f"RENAME TO {_quote_ident(new_name)}"
        )
    )


def _configure_partman(bind) -> None:
    created = bind.execute(
        sa.text(
            """
            SELECT partman.create_parent(
                p_parent_table := :parent_table,
                p_control := 'created_at',
                p_interval := '1 month',
                p_type := 'range',
                p_epoch := 'none',
                p_premake := :premake,
                p_start_partition := :start_partition,
                p_default_table := false,
                p_automatic_maintenance := 'on',
                p_template_table := 'false',
                p_jobmon := false
            )
            """
        ),
        {
            "parent_table": _PARENT,
            "premake": _PREMAKE,
            "start_partition": _START_PARTITION,
        },
    ).scalar_one()
    if created is not True:
        raise RuntimeError(
            "pg_partman.create_parent did not confirm auth-session "
            "partition registration."
        )

    result = bind.execute(
        sa.text(
            """
            UPDATE partman.part_config
            SET
                premake = :premake,
                automatic_maintenance = 'on',
                infinite_time_partitions = true,
                retention = NULL,
                retention_keep_table = true,
                jobmon = false
            WHERE parent_table = :parent_table
            """
        ),
        {
            "parent_table": _PARENT,
            "premake": _PREMAKE,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "Expected exactly one pg_partman config row for auth_sessions; "
            f"observed rowcount={result.rowcount}."
        )


def _require_partman_config(bind) -> None:
    config = _fetch_one(
        bind,
        """
        SELECT
            parent_table::text AS parent_table,
            control::text AS control,
            (partition_interval::interval = interval '1 month')
                AS partition_interval_matches,
            partition_type::text AS partition_type,
            premake,
            automatic_maintenance::text AS automatic_maintenance,
            template_table::text AS template_table,
            retention IS NULL AS retention_is_null,
            retention_keep_table,
            infinite_time_partitions,
            default_table,
            jobmon
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )
    expected = {
        "parent_table": _PARENT,
        "control": "created_at",
        "partition_interval_matches": True,
        "partition_type": "range",
        "premake": _PREMAKE,
        "automatic_maintenance": "on",
        "template_table": None,
        "retention_is_null": True,
        "retention_keep_table": True,
        "infinite_time_partitions": True,
        "default_table": False,
        "jobmon": False,
    }
    if config != expected:
        raise RuntimeError(
            "Auth-session pg_partman configuration drifted: "
            f"observed={config!r}, expected={expected!r}."
        )


def _require_current_partition(bind) -> None:
    current = _fetch_one(
        bind,
        """
        SELECT
            partition_schema::text AS partition_schema,
            partition_table::text AS partition_table,
            table_exists
        FROM partman.show_partition_name(
            :parent_table,
            CURRENT_TIMESTAMP::text
        )
        """,
        {"parent_table": _PARENT},
    )
    if current is None or not current["table_exists"]:
        raise RuntimeError(
            "pg_partman registration did not provision a concrete current "
            f"auth-session partition: {current!r}."
        )
    if current["partition_schema"] != _PARENT_SCHEMA:
        raise RuntimeError(
            "Current auth-session partition resolved outside public schema: "
            f"{current!r}."
        )
    name = current["partition_table"] or ""
    if not _GENERATED_CHILD_RE.fullmatch(name):
        raise RuntimeError(
            "Current auth-session partition has an unexpected name: "
            f"{current!r}."
        )

    default_relation = bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:relation_name)::text"),
        {"relation_name": _DEFAULT_RELATION},
    ).scalar_one()
    if default_relation is not None:
        raise RuntimeError(
            "Auth-session adoption unexpectedly created a DEFAULT partition: "
            f"{default_relation!r}."
        )


def _require_downgrade_safe(bind) -> list[str]:
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    bind.execute(sa.text(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE"))
    _require_partman_config(bind)

    rows = _partition_rows(bind)
    by_name = {row["child_name"]: row for row in rows}
    legacy = by_name.get(_CANONICAL_LEGACY_CHILD)
    if legacy is None:
        raise RuntimeError(
            "Cannot downgrade auth-session partition adoption because the "
            "canonical historical May-2026 child is missing."
        )
    if legacy["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "Cannot downgrade auth-session partition adoption from an "
            f"unexpected child owner: {legacy!r}."
        )
    legacy_bound = legacy["partition_bound"] or ""
    if "2026-05-01" not in legacy_bound or "2026-06-01" not in legacy_bound:
        raise RuntimeError(
            "Cannot downgrade auth-session partition adoption because the "
            f"historical child bound drifted: {legacy_bound!r}."
        )

    removable: list[str] = []
    for row in rows:
        name = row["child_name"]
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(
                "Cannot downgrade auth-session partition adoption while a "
                f"child has unexpected ownership: {row!r}."
            )
        if name == _CANONICAL_LEGACY_CHILD:
            continue
        if not _GENERATED_CHILD_RE.fullmatch(name):
            raise RuntimeError(
                "Cannot downgrade auth-session partition adoption with an "
                f"unknown child relation: {row!r}."
            )

        row_count = bind.execute(
            sa.text(f"SELECT count(*) FROM {_qualified(name)}")
        ).scalar_one()
        if row_count != 0:
            raise RuntimeError(
                "Refusing to downgrade auth-session partition adoption because "
                f"{name} contains {row_count} row(s). Session data must never "
                "be dropped by rollback."
            )
        removable.append(name)

    return removable


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    _lock_and_require_predecessor(bind)
    _rename_legacy_child(bind, _LEGACY_CHILD, _CANONICAL_LEGACY_CHILD)
    _configure_partman(bind)
    _require_partman_config(bind)
    _require_current_partition(bind)


def downgrade() -> None:
    bind = op.get_bind()
    removable = _require_downgrade_safe(bind)

    deleted = bind.execute(
        sa.text(
            "DELETE FROM partman.part_config "
            "WHERE parent_table = :parent_table"
        ),
        {"parent_table": _PARENT},
    )
    if deleted.rowcount != 1:
        raise RuntimeError(
            "Expected exactly one pg_partman config row while downgrading "
            f"auth_sessions; observed rowcount={deleted.rowcount}."
        )

    for name in removable:
        bind.execute(sa.text(f"DROP TABLE {_qualified(name)}"))

    _rename_legacy_child(
        bind,
        _CANONICAL_LEGACY_CHILD,
        _LEGACY_CHILD,
    )
