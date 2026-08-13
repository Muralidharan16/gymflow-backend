"""Add canonical geo references to organization branches.

Revision ID: 361c32e72e93
Revises: 16c65fdfd9a8
Create Date: 2026-06-06 21:26:15.174049

This revision is intentionally limited to its four nullable branch geo
references. Every immediate-predecessor relation, row, constraint, index,
policy, trigger, function, and view remains in place.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "361c32e72e93"
down_revision: Union[str, Sequence[str], None] = "16c65fdfd9a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MIGRATION_OWNER = "migration_owner"
_SCHEMA = "public"
_TABLES = (
    "org_branches",
    "countries",
    "subdivisions",
    "cities",
    "postal_codes",
)
_GEO_COLUMNS = {
    "geo_country_id": "smallint",
    "geo_subdivision_id": "bigint",
    "geo_city_id": "bigint",
    "geo_postal_code_id": "bigint",
}
_REQUIRED_PARENT_COLUMNS = {
    ("countries", "id"): "smallint",
    ("subdivisions", "id"): "bigint",
    ("subdivisions", "country_id"): "smallint",
    ("cities", "id"): "bigint",
    ("cities", "country_id"): "smallint",
    ("postal_codes", "id"): "bigint",
}
_REQUIRED_PARENT_KEYS = {
    ("countries", ("id",)),
    ("subdivisions", ("id",)),
    ("subdivisions", ("id", "country_id")),
    ("cities", ("id",)),
    ("cities", ("id", "country_id")),
    ("postal_codes", ("id",)),
}
_GEO_FOREIGN_KEYS = (
    (
        "org_branches_geo_country_id_fkey",
        ("geo_country_id",),
        "countries",
        ("id",),
    ),
    (
        "org_branches_geo_subdivision_id_fkey",
        ("geo_subdivision_id",),
        "subdivisions",
        ("id",),
    ),
    (
        "fk_org_branch_subdivision_country",
        ("geo_subdivision_id", "geo_country_id"),
        "subdivisions",
        ("id", "country_id"),
    ),
    (
        "org_branches_geo_city_id_fkey",
        ("geo_city_id",),
        "cities",
        ("id",),
    ),
    (
        "fk_org_branch_city_country",
        ("geo_city_id", "geo_country_id"),
        "cities",
        ("id", "country_id"),
    ),
    (
        "org_branches_geo_postal_code_id_fkey",
        ("geo_postal_code_id",),
        "postal_codes",
        ("id",),
    ),
)


def _require_migration_owner(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()
    observed = (row["session_user_name"], row["current_user_name"])
    expected = (_MIGRATION_OWNER, _MIGRATION_OWNER)
    if observed != expected:
        raise RuntimeError(
            "Revision 361 requires session_user=current_user="
            f"{_MIGRATION_OWNER}; observed {observed!r}."
        )


def _column_catalog(bind) -> dict[str, dict[str, object]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.relname::text AS table_name,
                relation_data.relkind::text AS relation_kind,
                pg_catalog.pg_get_userbyid(
                    relation_data.relowner
                )::text AS owner_name,
                attribute_data.attname::text AS column_name,
                pg_catalog.format_type(
                    attribute_data.atttypid,
                    attribute_data.atttypmod
                )::text AS data_type,
                (NOT attribute_data.attnotnull) AS is_nullable,
                pg_catalog.pg_get_expr(
                    default_data.adbin,
                    default_data.adrelid
                )::text AS default_expression
            FROM pg_catalog.pg_class AS relation_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            JOIN pg_catalog.pg_attribute AS attribute_data
              ON attribute_data.attrelid = relation_data.oid
            LEFT JOIN pg_catalog.pg_attrdef AS default_data
              ON default_data.adrelid = attribute_data.attrelid
             AND default_data.adnum = attribute_data.attnum
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname IN (
                    'org_branches',
                    'countries',
                    'subdivisions',
                    'cities',
                    'postal_codes'
              )
              AND attribute_data.attnum > 0
              AND NOT attribute_data.attisdropped
            ORDER BY relation_data.relname, attribute_data.attnum
            """
        )
    ).mappings().all()
    catalog: dict[str, dict[str, object]] = {}
    for row in rows:
        table = catalog.setdefault(
            row["table_name"],
            {
                "kind": row["relation_kind"],
                "owner": row["owner_name"],
                "columns": {},
            },
        )
        table["columns"][row["column_name"]] = {
            "type": row["data_type"],
            "nullable": bool(row["is_nullable"]),
            "default": row["default_expression"],
        }
    return catalog


def _constraint_catalog(bind) -> list[dict[str, object]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                relation_data.relname::text AS table_name,
                constraint_data.conname::text AS constraint_name,
                constraint_data.contype::text AS constraint_type,
                parent_namespace.nspname::text AS parent_schema,
                parent_relation.relname::text AS parent_table,
                constraint_data.confdeltype::text AS delete_action,
                constraint_data.convalidated AS is_validated,
                constraint_data.condeferrable AS is_deferrable,
                constraint_data.condeferred AS is_initially_deferred,
                ARRAY(
                    SELECT local_attribute.attname::text
                    FROM pg_catalog.unnest(
                        constraint_data.conkey
                    ) WITH ORDINALITY AS local_key(attnum, position)
                    JOIN pg_catalog.pg_attribute AS local_attribute
                      ON local_attribute.attrelid = constraint_data.conrelid
                     AND local_attribute.attnum = local_key.attnum
                    ORDER BY local_key.position
                ) AS local_columns,
                ARRAY(
                    SELECT parent_attribute.attname::text
                    FROM pg_catalog.unnest(
                        constraint_data.confkey
                    ) WITH ORDINALITY AS parent_key(attnum, position)
                    JOIN pg_catalog.pg_attribute AS parent_attribute
                      ON parent_attribute.attrelid = constraint_data.confrelid
                     AND parent_attribute.attnum = parent_key.attnum
                    ORDER BY parent_key.position
                ) AS parent_columns
            FROM pg_catalog.pg_constraint AS constraint_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = constraint_data.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            LEFT JOIN pg_catalog.pg_class AS parent_relation
              ON parent_relation.oid = constraint_data.confrelid
            LEFT JOIN pg_catalog.pg_namespace AS parent_namespace
              ON parent_namespace.oid = parent_relation.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname IN (
                    'org_branches',
                    'countries',
                    'subdivisions',
                    'cities',
                    'postal_codes'
              )
            ORDER BY relation_data.relname, constraint_data.conname
            """
        )
    ).mappings().all()
    result = []
    for row in rows:
        item = dict(row)
        item["local_columns"] = tuple(item["local_columns"] or ())
        item["parent_columns"] = tuple(item["parent_columns"] or ())
        result.append(item)
    return result


def _verify_contract(bind, *, geo_present: bool) -> None:
    columns = _column_catalog(bind)
    constraints = _constraint_catalog(bind)

    if set(columns) != set(_TABLES):
        raise RuntimeError(
            "Revision-361 predecessor relation inventory drift: "
            f"{sorted(columns)!r}."
        )
    for table_name, table in columns.items():
        if table["kind"] not in {"r", "p"}:
            raise RuntimeError(
                f"public.{table_name} is not a table: {table['kind']!r}."
            )
        if table["owner"] != _MIGRATION_OWNER:
            raise RuntimeError(
                f"public.{table_name} owner drift: {table['owner']!r}."
            )

    for (table_name, column_name), data_type in (
        _REQUIRED_PARENT_COLUMNS.items()
    ):
        column = columns[table_name]["columns"].get(column_name)
        if (
            column is None
            or column["type"] != data_type
            or column["nullable"]
        ):
            raise RuntimeError(
                f"public.{table_name}.{column_name} contract drift: "
                f"{column!r}."
            )

    available_keys = {
        (item["table_name"], tuple(item["local_columns"]))
        for item in constraints
        if item["constraint_type"] in {"p", "u"}
        and bool(item["is_validated"])
        and not bool(item["is_deferrable"])
    }
    missing_keys = _REQUIRED_PARENT_KEYS - available_keys
    if missing_keys:
        raise RuntimeError(
            f"Revision-361 required parent keys are absent: {missing_keys!r}."
        )

    branch_columns = columns["org_branches"]["columns"]
    for column_name, data_type in _GEO_COLUMNS.items():
        column = branch_columns.get(column_name)
        if not geo_present:
            if column is not None:
                raise RuntimeError(
                    f"public.org_branches.{column_name} already exists."
                )
            continue
        if (
            column is None
            or column["type"] != data_type
            or not column["nullable"]
            or column["default"] is not None
        ):
            raise RuntimeError(
                f"public.org_branches.{column_name} contract drift: "
                f"{column!r}."
            )

    geo_names = {item[0] for item in _GEO_FOREIGN_KEYS}
    named = {
        item["constraint_name"]: item
        for item in constraints
        if item["table_name"] == "org_branches"
        and item["constraint_name"] in geo_names
    }
    related = [
        item
        for item in constraints
        if item["table_name"] == "org_branches"
        and item["constraint_type"] == "f"
        and set(_GEO_COLUMNS).intersection(item["local_columns"])
    ]
    if not geo_present:
        if named or related:
            raise RuntimeError(
                "Revision-361 geo foreign-key collision in predecessor state."
            )
        return
    if len(related) != len(_GEO_FOREIGN_KEYS):
        raise RuntimeError(
            "Revision-361 geo foreign-key cardinality drift: "
            f"{len(related)}."
        )

    for name, local_columns, parent_table, parent_columns in _GEO_FOREIGN_KEYS:
        constraint = named.get(name)
        observed = None
        if constraint is not None:
            observed = (
                constraint["constraint_type"],
                tuple(constraint["local_columns"]),
                constraint["parent_schema"],
                constraint["parent_table"],
                tuple(constraint["parent_columns"]),
                constraint["delete_action"],
                bool(constraint["is_validated"]),
                bool(constraint["is_deferrable"]),
                bool(constraint["is_initially_deferred"]),
            )
        expected = (
            "f",
            local_columns,
            _SCHEMA,
            parent_table,
            parent_columns,
            "r",
            True,
            False,
            False,
        )
        if observed != expected:
            raise RuntimeError(
                f"Geo foreign key {name} contract drift: {observed!r}."
            )


def _preflight(bind, *, direction: str) -> None:
    _require_migration_owner(bind)
    if direction == "upgrade":
        _verify_contract(bind, geo_present=False)
    elif direction == "downgrade":
        _verify_contract(bind, geo_present=True)
    else:
        raise RuntimeError(f"Unsupported revision-361 direction {direction!r}.")


def _postflight(bind, *, direction: str) -> None:
    if direction == "upgrade":
        _verify_contract(bind, geo_present=True)
    elif direction == "downgrade":
        _verify_contract(bind, geo_present=False)
    else:
        raise RuntimeError(f"Unsupported revision-361 direction {direction!r}.")


def upgrade() -> None:
    """Add the four nullable canonical-geo references to org_branches."""
    bind = op.get_bind()
    _preflight(bind, direction="upgrade")

    op.add_column(
        "org_branches",
        sa.Column("geo_country_id", sa.SmallInteger(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "org_branches",
        sa.Column("geo_subdivision_id", sa.BigInteger(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "org_branches",
        sa.Column("geo_city_id", sa.BigInteger(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "org_branches",
        sa.Column("geo_postal_code_id", sa.BigInteger(), nullable=True),
        schema=_SCHEMA,
    )

    for name, local_columns, parent_table, parent_columns in _GEO_FOREIGN_KEYS:
        op.create_foreign_key(
            name,
            "org_branches",
            parent_table,
            list(local_columns),
            list(parent_columns),
            source_schema=_SCHEMA,
            referent_schema=_SCHEMA,
            ondelete="RESTRICT",
        )

    _postflight(bind, direction="upgrade")


def downgrade() -> None:
    """Remove only the geo references introduced by this revision."""
    bind = op.get_bind()
    _preflight(bind, direction="downgrade")

    for name, _local, _parent_table, _parent_columns in reversed(
        _GEO_FOREIGN_KEYS
    ):
        op.drop_constraint(
            name,
            "org_branches",
            type_="foreignkey",
            schema=_SCHEMA,
        )

    op.drop_column("org_branches", "geo_postal_code_id", schema=_SCHEMA)
    op.drop_column("org_branches", "geo_city_id", schema=_SCHEMA)
    op.drop_column("org_branches", "geo_subdivision_id", schema=_SCHEMA)
    op.drop_column("org_branches", "geo_country_id", schema=_SCHEMA)

    _postflight(bind, direction="downgrade")
