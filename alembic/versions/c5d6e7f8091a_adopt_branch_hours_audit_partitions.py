"""Adopt branch-hours audit partitions into infrastructure-owned pg_partman.

Revision ID: c5d6e7f8091a
Revises: b4c5d6e7f809
Create Date: 2026-08-11

The application previously attempted CREATE TABLE ... PARTITION OF during
FastAPI startup and from a Celery task. That is incompatible with the reduced
runtime boundary: app_runtime intentionally has no database/schema CREATE
capability and must never become a DDL identity merely to keep audit inserts
working across a month boundary.

pg_partman 5.0.1 is already an externally provisioned infrastructure dependency
for lifecycle partitions. This revision adopts the existing declarative
branch_hours_audit_log parent into that same control plane. The historical May
2026 partition is renamed into pg_partman's canonical naming convention without
moving data. pg_partman creates and owns its normal template metadata, creates
current/future monthly children plus a DEFAULT safety partition, and maintains
four months ahead. ``infinite_time_partitions`` is enabled so a quiet audit
stream cannot let future coverage expire.

No retention is configured here: audit retention is a governance decision and
must not be silently invented by infrastructure hardening. Downgrade refuses to
discard any data that has landed in revision-created/default partitions, then
uses pg_partman's supported config cleanup path to remove both configuration and
the managed template without touching the partitioned parent.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c5d6e7f8091a"
down_revision = "b4c5d6e7f809"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_PARTMAN_SCHEMA = "partman"
_PARTMAN_VERSION = "5.0.1"
_PARENT_SCHEMA = "public"
_PARENT_NAME = "branch_hours_audit_log"
_PARENT = f"{_PARENT_SCHEMA}.{_PARENT_NAME}"
_TEMPLATE = "partman.template_public_branch_hours_audit_log"
_LEGACY_TO_PARTMAN = (
    ("branch_hours_audit_log_y2026m05", "branch_hours_audit_log_p20260501"),
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
            role_data.rolsuper,
            role_data.rolinherit,
            role_data.rolcreatedb,
            role_data.rolcreaterole,
            role_data.rolreplication,
            role_data.rolbypassrls
        FROM pg_catalog.pg_roles AS role_data
        WHERE role_data.rolname = current_user
        """,
    )
    if row is None:
        raise RuntimeError("Unable to resolve migration identity")
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "c5d6 requires session_user=current_user=migration_owner"
        )
    if any(
        bool(row[key])
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


def _require_partman_dependency(bind) -> None:
    extension = _fetch_one(
        bind,
        """
        SELECT
            extension_data.extversion::text AS version,
            namespace_data.nspname::text AS schema_name,
            owner_role.rolname::text AS owner_name
        FROM pg_catalog.pg_extension AS extension_data
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = extension_data.extnamespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = extension_data.extowner
        WHERE extension_data.extname = 'pg_partman'
        """,
    )
    if extension is None:
        raise RuntimeError(
            "Required infrastructure-owned pg_partman extension is absent"
        )
    if extension["version"] != _PARTMAN_VERSION:
        raise RuntimeError(
            "Unsupported pg_partman version for c5d6: "
            f"observed={extension['version']!r}, required={_PARTMAN_VERSION!r}"
        )
    if extension["schema_name"] != _PARTMAN_SCHEMA:
        raise RuntimeError(
            f"pg_partman must be installed in schema {_PARTMAN_SCHEMA}"
        )
    if extension["owner_name"] == _MIGRATION_OWNER:
        raise RuntimeError("pg_partman must remain infrastructure-owned")

    routines = (
        "partman.create_parent(text,text,text,text,text,integer,text,boolean,text,text[],text,boolean,text)",
        "partman.run_maintenance(text,boolean,boolean)",
        "partman.config_cleanup(text,boolean,boolean,boolean)",
    )
    for signature in routines:
        row = _fetch_one(
            bind,
            """
            WITH requested AS (
                SELECT pg_catalog.to_regprocedure(:signature) AS routine_oid
            )
            SELECT
                requested.routine_oid IS NOT NULL AS exists,
                CASE
                    WHEN requested.routine_oid IS NULL THEN FALSE
                    ELSE pg_catalog.has_function_privilege(
                        current_user, requested.routine_oid, 'EXECUTE'
                    )
                END AS can_execute
            FROM requested
            """,
            {"signature": signature},
        )
        if row != {"exists": True, "can_execute": True}:
            raise RuntimeError(
                f"migration_owner lacks required pg_partman routine: {signature}"
            )

    privileges = _fetch_one(
        bind,
        """
        SELECT
            pg_catalog.has_schema_privilege(current_user, 'partman', 'USAGE') AS schema_usage,
            pg_catalog.has_table_privilege(current_user, 'partman.part_config', 'SELECT') AS config_select,
            pg_catalog.has_table_privilege(current_user, 'partman.part_config', 'INSERT') AS config_insert,
            pg_catalog.has_table_privilege(current_user, 'partman.part_config', 'UPDATE') AS config_update,
            pg_catalog.has_table_privilege(current_user, 'partman.part_config', 'DELETE') AS config_delete
        """,
    )
    if privileges is None or not all(privileges.values()):
        raise RuntimeError(
            "migration_owner lacks bounded pg_partman configuration privileges: "
            f"{privileges!r}"
        )


def _lock_and_require_predecessor(bind) -> None:
    bind.execute(sa.text(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE"))

    parent = _fetch_one(
        bind,
        """
        SELECT
            relation_data.relkind::text AS relation_kind,
            pg_catalog.pg_get_userbyid(relation_data.relowner)::text AS owner_name,
            relation_data.relrowsecurity AS rls_enabled,
            relation_data.relforcerowsecurity AS force_rls,
            pg_catalog.pg_get_partkeydef(relation_data.oid)::text AS partition_key
        FROM pg_catalog.pg_class AS relation_data
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = relation_data.relnamespace
        WHERE namespace_data.nspname = :schema_name
          AND relation_data.relname = :relation_name
        """,
        {"schema_name": _PARENT_SCHEMA, "relation_name": _PARENT_NAME},
    )
    expected_parent = {
        "relation_kind": "p",
        "owner_name": _MIGRATION_OWNER,
        "rls_enabled": True,
        "force_rls": True,
        "partition_key": "RANGE (changed_at)",
    }
    if parent != expected_parent:
        raise RuntimeError(
            "c5d6 predecessor parent contract drifted: "
            f"observed={parent!r}, expected={expected_parent!r}"
        )

    config = _fetch_one(
        bind,
        """
        SELECT count(*)::integer AS config_count
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )
    if config != {"config_count": 0}:
        raise RuntimeError(
            f"c5d6 predecessor already has pg_partman configuration: {config!r}"
        )

    template_conflict = _fetch_one(
        bind,
        """
        SELECT pg_catalog.to_regclass(:template_name) IS NOT NULL AS template_exists
        """,
        {"template_name": _TEMPLATE},
    )
    if template_conflict != {"template_exists": False}:
        raise RuntimeError(
            f"c5d6 predecessor already has managed template {_TEMPLATE}"
        )

    children = _fetch_all(
        bind,
        """
        SELECT
            child.relname::text AS child_name,
            pg_catalog.pg_get_expr(child.relpartbound, child.oid, true)::text AS partition_bound
        FROM pg_catalog.pg_inherits AS inheritance
        JOIN pg_catalog.pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = parent.relnamespace
        JOIN pg_catalog.pg_class AS child
          ON child.oid = inheritance.inhrelid
        WHERE namespace_data.nspname = :schema_name
          AND parent.relname = :relation_name
        ORDER BY child.relname
        """,
        {"schema_name": _PARENT_SCHEMA, "relation_name": _PARENT_NAME},
    )
    if len(children) != 1:
        raise RuntimeError(
            "c5d6 predecessor partition inventory drifted: "
            f"{children!r}"
        )
    child = children[0]
    if child["child_name"] != _LEGACY_TO_PARTMAN[0][0]:
        raise RuntimeError(
            f"unexpected predecessor audit partition: {child['child_name']!r}"
        )
    bound = child["partition_bound"] or ""
    if "2026-05-01" not in bound or "2026-06-01" not in bound:
        raise RuntimeError(
            f"legacy audit partition bounds drifted: {bound!r}"
        )

    conflict = _fetch_one(
        bind,
        """
        SELECT count(*)::integer AS conflict_count
        FROM pg_catalog.pg_class AS relation_data
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = relation_data.relnamespace
        WHERE namespace_data.nspname = :schema_name
          AND relation_data.relname = :target_name
        """,
        {
            "schema_name": _PARENT_SCHEMA,
            "target_name": _LEGACY_TO_PARTMAN[0][1],
        },
    )
    if conflict != {"conflict_count": 0}:
        raise RuntimeError("canonical pg_partman audit partition name already exists")


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
                p_control := 'changed_at',
                p_interval := '1 month',
                p_type := 'range',
                p_epoch := 'none',
                p_premake := 4,
                p_start_partition := '2026-05-01 00:00:00+00',
                p_default_table := true,
                p_automatic_maintenance := 'on',
                p_jobmon := false
            )
            """
        ),
        {"parent_table": _PARENT},
    ).scalar_one()
    if created is not True:
        raise RuntimeError("pg_partman did not confirm audit parent registration")

    result = bind.execute(
        sa.text(
            """
            UPDATE partman.part_config
            SET infinite_time_partitions = true
            WHERE parent_table = :parent_table
            """
        ),
        {"parent_table": _PARENT},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "expected one pg_partman audit config row while enabling infinite_time_partitions"
        )

    bind.execute(
        sa.text(
            """
            SELECT partman.run_maintenance(
                p_parent_table := :parent_table,
                p_analyze := false,
                p_jobmon := false
            )
            """
        ),
        {"parent_table": _PARENT},
    )


def _config(bind):
    return _fetch_one(
        bind,
        """
        SELECT
            parent_table::text AS parent_table,
            control::text AS control,
            (partition_interval::interval = interval '1 month') AS interval_matches,
            partition_type::text AS partition_type,
            premake,
            automatic_maintenance::text AS automatic_maintenance,
            template_table::text AS template_table,
            retention::text AS retention,
            default_table,
            jobmon,
            infinite_time_partitions
        FROM partman.part_config
        WHERE parent_table = :parent_table
        """,
        {"parent_table": _PARENT},
    )


def _expected_config():
    return {
        "parent_table": _PARENT,
        "control": "changed_at",
        "interval_matches": True,
        "partition_type": "range",
        "premake": 4,
        "automatic_maintenance": "on",
        "template_table": _TEMPLATE,
        "retention": None,
        "default_table": True,
        "jobmon": False,
        "infinite_time_partitions": True,
    }


def _require_current_and_future_coverage(bind) -> None:
    missing = bind.execute(
        sa.text(
            """
            WITH required AS (
                SELECT
                    offset_value,
                    'branch_hours_audit_log_p' ||
                    pg_catalog.to_char(
                        pg_catalog.date_trunc('month', pg_catalog.current_timestamp)
                        + pg_catalog.make_interval(months => offset_value),
                        'YYYYMMDD'
                    ) AS child_name
                FROM pg_catalog.generate_series(0, 4) AS offset_value
            )
            SELECT required.child_name
            FROM required
            WHERE pg_catalog.to_regclass('public.' || required.child_name) IS NULL
            ORDER BY required.offset_value
            """
        )
    ).scalars().all()
    if missing:
        raise RuntimeError(
            f"pg_partman audit partition coverage is incomplete: {missing!r}"
        )

    default_count = bind.execute(
        sa.text(
            """
            SELECT count(*)::integer
            FROM pg_catalog.pg_inherits AS inheritance
            JOIN pg_catalog.pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = parent.relnamespace
            JOIN pg_catalog.pg_class AS child
              ON child.oid = inheritance.inhrelid
            WHERE namespace_data.nspname = 'public'
              AND parent.relname = 'branch_hours_audit_log'
              AND pg_catalog.pg_get_expr(child.relpartbound, child.oid, true) = 'DEFAULT'
            """
        )
    ).scalar_one()
    if default_count != 1:
        raise RuntimeError(
            f"expected exactly one audit DEFAULT partition, observed {default_count}"
        )


def _verify_forward(bind) -> None:
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    observed = _config(bind)
    expected = _expected_config()
    if observed != expected:
        raise RuntimeError(
            "pg_partman audit configuration drifted: "
            f"observed={observed!r}, expected={expected!r}"
        )

    template = _fetch_one(
        bind,
        """
        SELECT
            pg_catalog.pg_get_userbyid(relation_data.relowner)::text AS owner_name,
            relation_data.relkind::text AS relation_kind
        FROM pg_catalog.pg_class AS relation_data
        WHERE relation_data.oid = pg_catalog.to_regclass(:template_name)
        """,
        {"template_name": _TEMPLATE},
    )
    if template != {"owner_name": _MIGRATION_OWNER, "relation_kind": "r"}:
        raise RuntimeError(
            "pg_partman managed audit template drifted: "
            f"observed={template!r}"
        )

    preserved = _fetch_one(
        bind,
        """
        SELECT count(*)::integer AS preserved_count
        FROM pg_catalog.pg_inherits AS inheritance
        JOIN pg_catalog.pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_catalog.pg_namespace AS namespace_data
          ON namespace_data.oid = parent.relnamespace
        JOIN pg_catalog.pg_class AS child
          ON child.oid = inheritance.inhrelid
        WHERE namespace_data.nspname = 'public'
          AND parent.relname = 'branch_hours_audit_log'
          AND child.relname = :child_name
        """,
        {"child_name": _LEGACY_TO_PARTMAN[0][1]},
    )
    if preserved != {"preserved_count": 1}:
        raise RuntimeError("preserved May 2026 audit partition is missing")
    _require_current_and_future_coverage(bind)


def _child_names(bind) -> list[str]:
    return [
        row["child_name"]
        for row in _fetch_all(
            bind,
            """
            SELECT child.relname::text AS child_name
            FROM pg_catalog.pg_inherits AS inheritance
            JOIN pg_catalog.pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = parent.relnamespace
            JOIN pg_catalog.pg_class AS child
              ON child.oid = inheritance.inhrelid
            WHERE namespace_data.nspname = :schema_name
              AND parent.relname = :relation_name
            ORDER BY child.relname
            """,
            {"schema_name": _PARENT_SCHEMA, "relation_name": _PARENT_NAME},
        )
    ]


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
            "Refusing destructive c5d6 downgrade because revision-created "
            f"audit partitions contain data: {populated!r}"
        )


def _remove_partman_management(bind) -> None:
    preserved = {_LEGACY_TO_PARTMAN[0][1]}
    extras = [name for name in _child_names(bind) if name not in preserved]
    _require_extra_partitions_empty(bind, extras)

    bind.execute(
        sa.text(
            """
            SELECT partman.config_cleanup(
                p_parent_table := :parent_table,
                p_config_table := true,
                p_config_sub_table := true,
                p_template_table := true
            )
            """
        ),
        {"parent_table": _PARENT},
    )

    if _config(bind) is not None:
        raise RuntimeError("pg_partman audit configuration survived cleanup")
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:template_name) IS NOT NULL"),
        {"template_name": _TEMPLATE},
    ).scalar_one():
        raise RuntimeError("pg_partman audit template survived cleanup")

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

    children = _child_names(bind)
    if children != [_LEGACY_TO_PARTMAN[0][0]]:
        raise RuntimeError(
            f"c5d6 downgrade partition inventory drifted: {children!r}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    _lock_and_require_predecessor(bind)
    _rename_partitions(bind, _LEGACY_TO_PARTMAN)
    _configure_partman(bind)
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_identity(bind)
    _require_partman_dependency(bind)
    bind.execute(sa.text(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE"))
    _verify_forward(bind)
    _remove_partman_management(bind)
