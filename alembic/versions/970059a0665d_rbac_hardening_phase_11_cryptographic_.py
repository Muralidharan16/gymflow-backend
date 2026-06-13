"""RBAC Hardening Phase 11 - Cryptographic Key Registry

Revision ID: 970059a0665d
Revises: 45df3b75ed74
Create Date: 2026-05-23 16:17:58.085724

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '970059a0665d'
down_revision: Union[str, Sequence[str], None] = '45df3b75ed74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.audit_key_registry (
        key_version         SMALLINT PRIMARY KEY,
        kms_key_alias       VARCHAR(128) NOT NULL,
        algorithm           VARCHAR(32) NOT NULL DEFAULT 'aes-256-gcm',
        digest_algorithm    VARCHAR(32) NOT NULL DEFAULT 'sha-256',
        signature_algorithm VARCHAR(32) NOT NULL DEFAULT 'hmac-sha-256',
        rotation_date       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        retirement_date     TIMESTAMPTZ NULL,
        is_active           BOOLEAN NOT NULL DEFAULT TRUE
    );
    """)

    op.execute("""
    INSERT INTO public.audit_key_registry (key_version, kms_key_alias)
    VALUES (1, 'alias/gymflow-audit-v1')
    ON CONFLICT (key_version) DO NOTHING;
    """)

    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'fk_branch_audit_log_hash_key'
        ) THEN
            ALTER TABLE public.branch_audit_log
                ADD CONSTRAINT fk_branch_audit_log_hash_key
                FOREIGN KEY (hash_key_version) REFERENCES public.audit_key_registry(key_version)
                ON DELETE RESTRICT;
        END IF;
    END
    $$;
    """)

def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE public.branch_audit_log DROP CONSTRAINT IF EXISTS fk_branch_audit_log_hash_key;")
    op.execute("DROP TABLE IF EXISTS public.audit_key_registry;")
