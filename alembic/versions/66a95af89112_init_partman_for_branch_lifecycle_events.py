"""Adopt branch lifecycle partitions into externally provisioned pg_partman.

Revision ID: 66a95af89112
Revises: df59095a360e
Create Date: 2026-05-24 09:53:10.686241

The pg_partman extension is cluster/database infrastructure and must be installed
before Alembic runs.  This revision only validates that dependency and safely
registers the existing declarative partition set without dropping data-bearing
partitions.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "66a95af89112"
down_revision: Union[str, Sequence[str], None] = "df59095a360e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MIGRATION_OWNER = "migration_owner"
_PARTMAN_EXTENSION = "pg_partman"
_PARTMAN_SCHEMA = "partman"
_PARTMAN_VERSION = "5.0.1"
_PARENT_SCHEMA = "public"
_PARENT_NAME = "branch_lifecycle_events"
_PARENT = f"{_PARENT_SCHEMA}.{_PARENT_NAME}"
_LEGACY_TO_PARTMAN = (
    (
        "branch_lifecycle_events_2026_q2",
        "branch_lifecycle_events_p20260401",
    ),
    (
        "branch_lifecycle_events_2026_q3",
        "branch_lifecycle_events_p20260701",
    ),
)


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
            "66a requires both session_user and current_user to be "
            f"{_MIGRATION_OWNER}; observed session={row['session_name']!r}, "
            f"current={row['current_name']!r}."
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
            "66a refuses an over-privileged migration_owner: "
            f"{unsafe!r}."
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
            "Unsupported pg_partman version for historical revision 66a: "
            f"observed={row['extension_version']!r}, "
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
            SELECT pg_catalog.to_regprocedure(
                :signature
            ) AS routine_oid
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
            "migration_owner lacks EXECUTE on the supported "
            "partman.create_parent signature."
        )

    privileges = _fetch_one(
        bind,
        """
        SELECT
            pg_catalog.has_schema_privilege(
                current_user,
                :schema_name,
                'USAGE'
            ) AS schema_usage,
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
        {"schema_name": _PARTMAN_SCHEMA},
    )
    if privileges is None or not all(privileges.values()):
        raise RuntimeError(
            "migration_owner lacks the bounded pg_partman privileges "
            f"required by revision 66a: {privileges!r}."
        )


def _lock_and_require_predecessor_partition_set(bind) -> None:
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
        "partition_key": "RANGE (emitted_at)",
    }
    if parent != expected_parent:
        raise RuntimeError(
            "66a predecessor parent-table contract drifted: "
            f"observed={parent!r}, expected={expected_parent!r}."
        )

    rows = _fetch_all(
        bind,
        """
        SELECT
            child.relname::text AS child_name,
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
        WHERE parent_namespace.nspname = :schema_name
          AND parent.relname = :relation_name
        ORDER BY child.relname
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "relation_name": _PARENT_NAME,
        },
    )
    observed_names = tuple(row["child_name"] for row in rows)
    expected_names = tuple(old for old, _ in _LEGACY_TO_PARTMAN)
    if observed_names != expected_names:
        raise RuntimeError(
            "66a predecessor partition membership drifted: "
            f"observed={observed_names!r}, expected={expected_names!r}."
        )

    expected_bound_fragments = {
        "branch_lifecycle_events_2026_q2": (
            "2026-04-01",
            "2026-07-01",
        ),
        "branch_lifecycle_events_2026_q3": (
            "2026-07-01",
            "2026-10-01",
        ),
    }
    for row in rows:
        bound = row["partition_bound"] or ""
        start, end = expected_bound_fragments[row["child_name"]]
        if start not in bound or end not in bound:
            raise RuntimeError(
                "66a predecessor partition bound drifted for "
                f"{row['child_name']}: {bound!r}."
            )

    conflicts = _fetch_all(
        bind,
        """
        SELECT relation.relname::text AS relation_name
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = :schema_name
          AND relation.relname = ANY(:target_names)
        ORDER BY relation.relname
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "target_names": [new for _, new in _LEGACY_TO_PARTMAN],
        },
    )
    if conflicts:
        raise RuntimeError(
            "Cannot safely rename predecessor partitions because target "
            f"relations already exist: {conflicts!r}."
        )


def _rename_partitions(bind, mapping) -> None:
    for old_name, new_name in mapping:
        bind.execute(
            sa.text(
                "ALTER TABLE "
                f"{_quote_ident(_PARENT_SCHEMA)}.{_quote_ident(old_name)} "
                f"RENAME TO {_quote_ident(new_name)}"
            )
        )


def _configure_partman(bind) -> None:
    created = bind.execute(
        sa.text(
            """
            SELECT partman.create_parent(
                p_parent_table := :parent_table,
                p_control := 'emitted_at',
                p_interval := '3 months',
                p_type := 'range',
                p_epoch := 'none',
                p_premake := 4,
                p_start_partition := '2026-04-01 00:00:00+00',
                p_default_table := false,
                p_automatic_maintenance := 'on',
                p_template_table := 'false',
                p_jobmon := false
            )
            """
        ),
        {"parent_table": _PARENT},
    ).scalar_one()
    if created is not True:
        raise RuntimeError(
            "partman.create_parent did not confirm successful registration."
        )

    result = bind.execute(
        sa.text(
            """
            UPDATE partman.part_config
            SET
                retention = '2 years',
                retention_keep_table = false
            WHERE parent_table = :parent_table
            """
        ),
        {"parent_table": _PARENT},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "Expected exactly one pg_partman configuration row while "
            f"setting retention; observed rowcount={result.rowcount}."
        )

    config = _fetch_one(
        bind,
        """
        SELECT
            parent_table::text AS parent_table,
            control::text AS control,
            (partition_interval::interval = interval '3 months')
                AS partition_interval_matches,
            partition_type::text AS partition_type,
            premake,
            automatic_maintenance::text AS automatic_maintenance,
            template_table::text AS template_table,
            (retention::interval = interval '2 years')
                AS retention_matches,
            retention_keep_table,
            default_table,
            jobmon
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )
    expected = {
        "parent_table": _PARENT,
        "control": "emitted_at",
        "partition_interval_matches": True,
        "partition_type": "range",
        "premake": 4,
        "automatic_maintenance": "on",
        "template_table": None,
        "retention_matches": True,
        "retention_keep_table": False,
        "default_table": False,
        "jobmon": False,
    }
    if config != expected:
        raise RuntimeError(
            "pg_partman configuration drifted after 66a registration: "
            f"observed={config!r}, expected={expected!r}."
        )


def _require_registered_partition_set(bind) -> None:
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    bind.execute(sa.text(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE"))

    config = _fetch_one(
        bind,
        """
        SELECT
            parent_table::text AS parent_table,
            control::text AS control,
            (partition_interval::interval = interval '3 months')
                AS partition_interval_matches,
            partition_type::text AS partition_type,
            premake,
            automatic_maintenance::text AS automatic_maintenance,
            template_table::text AS template_table,
            (retention::interval = interval '2 years')
                AS retention_matches,
            retention_keep_table,
            default_table,
            jobmon
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )
    expected = {
        "parent_table": _PARENT,
        "control": "emitted_at",
        "partition_interval_matches": True,
        "partition_type": "range",
        "premake": 4,
        "automatic_maintenance": "on",
        "template_table": None,
        "retention_matches": True,
        "retention_keep_table": False,
        "default_table": False,
        "jobmon": False,
    }
    if config != expected:
        raise RuntimeError(
            "Cannot downgrade 66a from an unexpected pg_partman "
            f"configuration: {config!r}."
        )

    names = {
        row["child_name"]
        for row in _fetch_all(
            bind,
            """
            SELECT child.relname::text AS child_name
            FROM pg_catalog.pg_inherits AS inheritance
            JOIN pg_catalog.pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = parent.relnamespace
            JOIN pg_catalog.pg_class AS child
              ON child.oid = inheritance.inhrelid
            WHERE namespace.nspname = :schema_name
              AND parent.relname = :relation_name
            ORDER BY child.relname
            """,
            {
                "schema_name": _PARENT_SCHEMA,
                "relation_name": _PARENT_NAME,
            },
        )
    }
    required = {new for _, new in _LEGACY_TO_PARTMAN}
    missing = sorted(required - names)
    if missing:
        raise RuntimeError(
            "Cannot downgrade 66a because required preserved partitions "
            f"are missing: {missing!r}."
        )


def _extra_partition_names(bind) -> list[str]:
    rows = _fetch_all(
        bind,
        """
        SELECT child.relname::text AS child_name
        FROM pg_catalog.pg_inherits AS inheritance
        JOIN pg_catalog.pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = parent.relnamespace
        JOIN pg_catalog.pg_class AS child
          ON child.oid = inheritance.inhrelid
        WHERE namespace.nspname = :schema_name
          AND parent.relname = :relation_name
        ORDER BY child.relname
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "relation_name": _PARENT_NAME,
        },
    )
    preserved = {new for _, new in _LEGACY_TO_PARTMAN}
    return [row["child_name"] for row in rows if row["child_name"] not in preserved]


def _require_extra_partitions_empty(bind, names: list[str]) -> None:
    populated = []
    for name in names:
        has_rows = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM ONLY "
                f"{_quote_ident(_PARENT_SCHEMA)}.{_quote_ident(name)} LIMIT 1)"
            )
        ).scalar_one()
        if has_rows:
            populated.append(name)
    if populated:
        raise RuntimeError(
            "Refusing destructive 66a downgrade because pg_partman-created "
            f"partitions contain data: {populated!r}."
        )


def _remove_partman_management(bind) -> None:
    extras = _extra_partition_names(bind)
    _require_extra_partitions_empty(bind, extras)

    result = bind.execute(
        sa.text(
            "DELETE FROM partman.part_config WHERE parent_table = :parent_table"
        ),
        {"parent_table": _PARENT},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "Expected exactly one pg_partman configuration row during "
            f"downgrade; observed rowcount={result.rowcount}."
        )

    for name in extras:
        bind.execute(
            sa.text(
                "DROP TABLE "
                f"{_quote_ident(_PARENT_SCHEMA)}.{_quote_ident(name)} RESTRICT"
            )
        )

    reverse_mapping = tuple(
        (new_name, old_name)
        for old_name, new_name in reversed(_LEGACY_TO_PARTMAN)
    )
    _rename_partitions(bind, reverse_mapping)

    remaining = _fetch_one(
        bind,
        """
        SELECT count(*)::integer AS config_count
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )
    if remaining != {"config_count": 0}:
        raise RuntimeError(
            "pg_partman configuration survived 66a downgrade: "
            f"{remaining!r}."
        )


def upgrade() -> None:
    """Safely adopt the predecessor's existing quarterly partition set."""
    bind = op.get_bind()
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    _lock_and_require_predecessor_partition_set(bind)
    _rename_partitions(bind, _LEGACY_TO_PARTMAN)
    _configure_partman(bind)


def downgrade() -> None:
    """Remove pg_partman management without discarding lifecycle-event data."""
    bind = op.get_bind()
    _require_registered_partition_set(bind)
    _remove_partman_management(bind)
