"""add_spatial_and_filtering_indexes

Revision ID: 371b1a44a333
Revises: 371b1a44a332
Create Date: 2026-05-18T14:22:12Z

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '371b1a44a333'
down_revision = '371b1a44a332'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 1. Determine if PostGIS extension is active
    bind = op.get_bind()
    try:
        res = bind.execute(sa.text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'postgis'")).scalar()
        has_postgis = (res > 0)
    except Exception:
        has_postgis = False

    # Explicit GIST index on organization_addresses.coordinates
    if has_postgis:
        op.create_index(
            'idx_org_addresses_coordinates',
            'organization_addresses',
            ['coordinates'],
            postgresql_using='gist'
        )
        
        # Explicit GIST index on member_addresses.coordinates
        op.create_index(
            'idx_member_addresses_coordinates',
            'member_addresses',
            ['coordinates'],
            postgresql_using='gist'
        )
    
    # Composite B-tree index on organization_addresses (country_code, state_province) where active
    op.create_index(
        'idx_org_addresses_country_state',
        'organization_addresses',
        ['country_code', 'state_province'],
        postgresql_where=sa.text('deleted_at IS NULL')
    )
    
    # Partial index for organization_addresses.city where active
    op.create_index(
        'idx_org_addresses_city',
        'organization_addresses',
        ['city'],
        postgresql_where=sa.text('deleted_at IS NULL')
    )

def downgrade() -> None:
    bind = op.get_bind()
    try:
        res = bind.execute(sa.text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'postgis'")).scalar()
        has_postgis = (res > 0)
    except Exception:
        has_postgis = False

    op.drop_index('idx_org_addresses_city', 'organization_addresses')
    op.drop_index('idx_org_addresses_country_state', 'organization_addresses')
    if has_postgis:
        op.drop_index('idx_member_addresses_coordinates', 'member_addresses')
        op.drop_index('idx_org_addresses_coordinates', 'organization_addresses')
