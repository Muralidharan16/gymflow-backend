"""RBAC Hardening Phase 19 - Transactional Outbox

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-05-23 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a1'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 19. Transactional Outbox
    op.execute("""
    CREATE TABLE public.transactional_outbox (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        event_type        VARCHAR(64) NOT NULL,
        payload           JSONB NOT NULL,
        dedupe_key        VARCHAR(128) NOT NULL,
        delivery_attempts SMALLINT NOT NULL DEFAULT 0,
        last_error        TEXT NULL,
        processed_at      TIMESTAMPTZ NULL,
        dead_lettered_at  TIMESTAMPTZ NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        leased_until      TIMESTAMPTZ NULL,
        leased_by         UUID NULL,
        CONSTRAINT uq_outbox_dedupe   UNIQUE (event_type, dedupe_key),
        CONSTRAINT chk_outbox_max_attempts CHECK (delivery_attempts <= 15)
    );
    """)

    op.execute("""
    CREATE INDEX ix_outbox_unprocessed ON public.transactional_outbox(created_at)
    WHERE processed_at IS NULL AND dead_lettered_at IS NULL;
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.transactional_outbox;")
