"""Validate canonical spatial indexes and add address filtering indexes.

Revision ID: 371b1a44a333
Revises: 371b1a44a332
Create Date: 2026-05-18T14:22:12Z

Revision 371b1a44a329 creates the Geography columns. GeoAlchemy2 owns the
corresponding GiST indexes through its spatial-index DDL listeners. This
revision must not create or drop those indexes a second time; it validates
them and owns only the filtering indexes introduced here.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "371b1a44a333"
down_revision = "371b1a44a332"
branch_labels = None
depends_on = None


_CANONICAL_SPATIAL_INDEXES = (
    (
        "organization_addresses",
        "idx_organization_addresses_coordinates",
    ),
    (
        "member_addresses",
        "idx_member_addresses_coordinates",
    ),
)


def _postgis_enabled(bind) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_extension
                    WHERE extname = 'postgis'
                )
                """
            )
        ).scalar_one()
    )


def _require_canonical_spatial_index(
    bind,
    *,
    table_name: str,
    index_name: str,
) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                index_relation.relname::text AS index_name,
                table_namespace.nspname::text AS table_schema,
                table_relation.relname::text AS table_name,
                access_method.amname::text AS access_method,
                index_data.indisvalid AS is_valid,
                index_data.indisready AS is_ready,
                index_data.indisunique AS is_unique,
                index_data.indpred IS NULL AS has_no_predicate,
                index_data.indexprs IS NULL AS has_no_expressions,
                ARRAY(
                    SELECT attribute.attname::text
                    FROM pg_catalog.unnest(
                        index_data.indkey::smallint[]
                    ) WITH ORDINALITY AS key_column(
                        attribute_number,
                        ordinal_position
                    )
                    JOIN pg_catalog.pg_attribute AS attribute
                      ON attribute.attrelid = table_relation.oid
                     AND attribute.attnum = key_column.attribute_number
                    WHERE key_column.attribute_number > 0
                    ORDER BY key_column.ordinal_position
                ) AS indexed_columns
            FROM pg_catalog.pg_index AS index_data
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_data.indexrelid
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_data.indrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            JOIN pg_catalog.pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE table_namespace.nspname = 'public'
              AND table_relation.relname = :table_name
              AND index_relation.relname = :index_name
            ORDER BY index_relation.oid
            """
        ),
        {
            "table_name": table_name,
            "index_name": index_name,
        },
    ).mappings().all()

    if len(rows) != 1:
        raise RuntimeError(
            "Expected exactly one GeoAlchemy2-owned spatial index "
            f"public.{index_name}; observed {len(rows)}."
        )

    observed = dict(rows[0])
    expected = {
        "index_name": index_name,
        "table_schema": "public",
        "table_name": table_name,
        "access_method": "gist",
        "is_valid": True,
        "is_ready": True,
        "is_unique": False,
        "has_no_predicate": True,
        "has_no_expressions": True,
        "indexed_columns": ["coordinates"],
    }
    if observed != expected:
        raise RuntimeError(
            "GeoAlchemy2-owned spatial index contract drifted: "
            f"observed={observed!r}, expected={expected!r}."
        )


def _require_no_redundant_org_spatial_index(bind) -> None:
    exists = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'public.idx_org_addresses_coordinates'
            ) IS NOT NULL
            """
        )
    ).scalar_one()
    if exists:
        raise RuntimeError(
            "Redundant spatial index public.idx_org_addresses_coordinates "
            "already exists; canonical ownership belongs to revision "
            "371b1a44a329/GeoAlchemy2."
        )


def _require_spatial_predecessor_contract(bind) -> None:
    if not _postgis_enabled(bind):
        return
    _require_no_redundant_org_spatial_index(bind)
    for table_name, index_name in _CANONICAL_SPATIAL_INDEXES:
        _require_canonical_spatial_index(
            bind,
            table_name=table_name,
            index_name=index_name,
        )


def upgrade() -> None:
    bind = op.get_bind()
    _require_spatial_predecessor_contract(bind)

    op.create_index(
        "idx_org_addresses_country_state",
        "organization_addresses",
        ["country_code", "state_province"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_org_addresses_city",
        "organization_addresses",
        ["city"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_spatial_predecessor_contract(bind)

    op.drop_index("idx_org_addresses_city", table_name="organization_addresses")
    op.drop_index(
        "idx_org_addresses_country_state",
        table_name="organization_addresses",
    )

    # The spatial indexes predate this revision and are intentionally preserved.
