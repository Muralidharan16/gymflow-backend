"""Set security_invoker on active branches view

Revision ID: 0009_view_security_invoker
Revises: 0008_force_rls
Create Date: 2026-05-22 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_view_security_invoker'
down_revision: Union[str, None] = '0008_force_rls'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = true);")


def downgrade() -> None:
    op.execute("ALTER VIEW v_active_org_branches SET (security_invoker = false);")
