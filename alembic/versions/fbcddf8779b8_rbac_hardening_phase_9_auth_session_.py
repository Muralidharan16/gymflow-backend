"""RBAC Hardening Phase 9 - auth session families

Revision ID: fbcddf8779b8
Revises: 0029_rbac_p8_contract
Create Date: 2026-05-23 15:55:38.936670

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'fbcddf8779b8'
down_revision: Union[str, Sequence[str], None] = '0029_rbac_p8_contract'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create auth_session_families
    op.create_table('auth_session_families',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 2. Create partitioned auth_sessions table
    op.execute("""
    CREATE TABLE public.auth_sessions (
        id                   UUID NOT NULL DEFAULT gen_random_uuid(),
        user_id              UUID NOT NULL,
        org_id               UUID NOT NULL,
        token_family_id      UUID NOT NULL REFERENCES public.auth_session_families(id) ON DELETE CASCADE,
        parent_session_id    UUID NULL,
        replaced_by_session_id UUID NULL,
        refresh_token_hash   VARCHAR(255) NOT NULL,
        device_info          JSONB NULL,
        ip_address           INET NULL,
        revoked_at           TIMESTAMPTZ NULL,
        last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        token_version_snapshot INT NOT NULL,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        deleted_at           TIMESTAMPTZ NULL,
        deleted_by           UUID NULL,
        reuse_detected_at    TIMESTAMPTZ NULL,
        compromised_at       TIMESTAMPTZ NULL,
        risk_score           SMALLINT NOT NULL DEFAULT 0,
        last_geo             VARCHAR(64) NULL,
        device_fingerprint   VARCHAR(128) NULL,
        PRIMARY KEY (id, created_at)
    ) PARTITION BY RANGE (created_at);
    """)
    
    # 3. Create index for replacing sessions
    op.execute("""
    CREATE UNIQUE INDEX uq_session_replacement
    ON public.auth_sessions(replaced_by_session_id, created_at)
    WHERE replaced_by_session_id IS NOT NULL;
    """)

    # Create initial partition for current month to avoid insertion errors
    # In a real system, pg_partman or similar would manage this.
    op.execute("""
    CREATE TABLE public.auth_sessions_y2026_m05 PARTITION OF public.auth_sessions
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.auth_sessions CASCADE")
    op.execute("DROP TABLE IF EXISTS public.auth_session_families CASCADE")
