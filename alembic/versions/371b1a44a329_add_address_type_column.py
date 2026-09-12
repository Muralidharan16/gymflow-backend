"""add_address_type_column

Revision ID: 371b1a44a329
Revises: 371b1a44a328
Create Date: 2026-05-18T14:18:25Z

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import geoalchemy2

# revision identifiers, used by Alembic.
revision = '371b1a44a329'
down_revision = '371b1a44a328'
branch_labels = None
depends_on = None


def _require_infrastructure_postgis() -> None:
    """Require deterministic, infrastructure-owned PostGIS state."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            """
            SELECT
                owner_role.rolname::text AS owner_name,
                namespace_data.nspname::text AS schema_name
            FROM pg_catalog.pg_extension AS extension_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = extension_data.extowner
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = extension_data.extnamespace
            WHERE extension_data.extname = 'postgis'
            """
        )
    ).mappings().first()
    if row is None:
        raise RuntimeError(
            "371b1a44a329 requires infrastructure-provisioned postgis; "
            "Alembic must not create extensions"
        )
    if row["owner_name"] != "postgres" or row["schema_name"] != "public":
        raise RuntimeError(
            "371b1a44a329 requires postgis owned by postgres in public; "
            f"observed owner={row['owner_name']!r}, schema={row['schema_name']!r}"
        )


def upgrade() -> None:
    # PostGIS is an infrastructure prerequisite, not migration-owned state.
    # The schema must therefore be deterministic across every environment:
    # never silently fall back from geography to VARCHAR when PostGIS is absent.
    _require_infrastructure_postgis()
    coord_col_type = geoalchemy2.types.Geography(
        geometry_type='POINT',
        srid=4326,
        from_text='ST_GeomFromEWKT',
        name='geography',
    )

    # Create organization_addresses table
    op.create_table(
        'organization_addresses',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('address_type', sa.String(50), nullable=False, server_default='operational'),
        sa.Column('address_line1', sa.Text(), nullable=False),
        sa.Column('address_line2', sa.Text(), nullable=True),
        sa.Column('city', sa.String(100), nullable=False),
        sa.Column('state_province', sa.String(100), nullable=False),
        sa.Column('postal_code', sa.String(15), nullable=True),
        sa.Column('country_code', sa.String(2), nullable=False),
        sa.Column('label', sa.String(100), nullable=True),
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('verified_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('verification_source', sa.String(100), nullable=True),
        sa.Column('coordinates', coord_col_type, nullable=True),
        sa.Column('coordinates_source', sa.String(100), nullable=True),
        sa.Column('is_exact_location_visible', sa.Boolean(), nullable=False, server_default=sa.text('TRUE')),
        sa.Column('formatted_address', sa.Text(), nullable=True),
        sa.Column('deleted_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()'))
    )

    op.create_check_constraint(
        'ck_org_address_type',
        'organization_addresses',
        sa.text("address_type IN ('registered', 'operational', 'billing')")
    )

    # Create member_addresses table
    op.create_table(
        'member_addresses',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('member_id', sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey('members.id', ondelete='CASCADE'), nullable=False),
        sa.Column('address_type', sa.String(50), nullable=False, server_default='operational'),
        sa.Column('address_line1', sa.Text(), nullable=False),
        sa.Column('address_line2', sa.Text(), nullable=True),
        sa.Column('city', sa.String(100), nullable=False),
        sa.Column('state_province', sa.String(100), nullable=False),
        sa.Column('postal_code', sa.String(15), nullable=True),
        sa.Column('country_code', sa.String(2), nullable=False),
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('verified_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('coordinates', coord_col_type, nullable=True),
        sa.Column('formatted_address', sa.Text(), nullable=True),
        sa.Column('deleted_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()'))
    )

    op.create_check_constraint(
        'ck_member_address_type',
        'member_addresses',
        sa.text("address_type IN ('registered', 'operational', 'billing')")
    )


def downgrade() -> None:
    op.drop_table('member_addresses')
    op.drop_table('organization_addresses')
