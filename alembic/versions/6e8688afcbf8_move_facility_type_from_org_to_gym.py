"""move facility_type from org to gym

Revision ID: 6e8688afcbf8
Revises: 165c866267fb
Create Date: 2026-05-13 13:03:41.552236

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6e8688afcbf8'
down_revision: Union[str, Sequence[str], None] = '165c866267fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add column as nullable first
    op.add_column('gyms', sa.Column('facility_type', sa.Enum('gym', 'yoga_studio', 'crossfit_box', 'swimming_pool', 'martial_arts', 'dance_studio', 'sports_academy', 'multi_sport', 'others', name='facilitytype'), nullable=True))
    
    # 2. Populate existing records from organization (best effort)
    op.execute("UPDATE gyms SET facility_type = organizations.facility_type FROM organizations WHERE gyms.org_id = organizations.id")
    
    # 3. Set default for any orphan records
    op.execute("UPDATE gyms SET facility_type = 'gym' WHERE facility_type IS NULL")
    
    # 4. Set to NOT NULL
    op.alter_column('gyms', 'facility_type', nullable=False)
    
    # 5. Drop from organizations
    op.drop_column('organizations', 'facility_type')


def downgrade() -> None:
    """Downgrade schema."""
    # 1. Add column back to organizations (nullable)
    op.add_column('organizations', sa.Column('facility_type', postgresql.ENUM('gym', 'yoga_studio', 'crossfit_box', 'swimming_pool', 'martial_arts', 'dance_studio', 'sports_academy', 'multi_sport', 'others', name='facilitytype'), nullable=True))
    
    # 2. Populate from gyms (pick first one)
    op.execute("UPDATE organizations SET facility_type = gyms.facility_type FROM gyms WHERE gyms.org_id = organizations.id")
    
    # 3. Set default
    op.execute("UPDATE organizations SET facility_type = 'gym' WHERE facility_type IS NULL")
    
    # 4. Set to NOT NULL
    op.alter_column('organizations', 'facility_type', nullable=False)
    
    # 5. Drop from gyms
    op.drop_column('gyms', 'facility_type')
