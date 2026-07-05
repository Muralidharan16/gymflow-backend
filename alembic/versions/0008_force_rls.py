"""Force RLS on branch tables

Revision ID: 0008_force_rls
Revises: 0007_fix_rbac_trigger
Create Date: 2026-05-22 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_force_rls'
down_revision: Union[str, None] = '0007_fix_rbac_trigger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE org_branches FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE org_branch_state FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    op.execute("ALTER TABLE org_branches NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE org_branch_state NO FORCE ROW LEVEL SECURITY;")
