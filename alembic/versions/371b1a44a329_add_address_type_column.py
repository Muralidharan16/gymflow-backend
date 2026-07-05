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

def upgrade() -> None:
    # 1. Determine if PostGIS is available on the system
    bind = op.get_bind()
    try:
        res = bind.execute(sa.text("SELECT COUNT(*) FROM pg_available_extensions WHERE name = 'postgis'")).scalar()
        has_postgis_extension = (res > 0)
    except Exception:
        has_postgis_extension = False

    if has_postgis_extension:
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            has_postgis = True
        except Exception:
            has_postgis = False
    else:
        has_postgis = False

    # Determine coordinate column type based on PostGIS availability
    if has_postgis:
        coord_col_type = geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, from_text='ST_GeomFromEWKT', name='geography')
    else:
        coord_col_type = sa.String(255)

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
