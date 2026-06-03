"""init_partman_for_branch_lifecycle_events

Revision ID: 66a95af89112
Revises: df59095a360e
Create Date: 2026-05-24 09:53:10.686241

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '66a95af89112'
down_revision: Union[str, Sequence[str], None] = 'df59095a360e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA IF NOT EXISTS partman;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;")
    op.execute("DROP TABLE IF EXISTS public.branch_lifecycle_events_2026_q2;")
    op.execute("DROP TABLE IF EXISTS public.branch_lifecycle_events_2026_q3;")
    op.execute("SELECT partman.create_parent(p_parent_table := 'public.branch_lifecycle_events', p_control := 'emitted_at', p_interval := '3 months');")
    # Also update retention to 24 months (2 years) as specified in blueprint
    op.execute("UPDATE partman.part_config SET retention = '2 years', retention_keep_table = false WHERE parent_table = 'public.branch_lifecycle_events';")

def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SELECT partman.undo_partition_proc('public.branch_lifecycle_events', p_keep_table := true);")
    op.execute("DELETE FROM partman.part_config WHERE parent_table = 'public.branch_lifecycle_events';")
