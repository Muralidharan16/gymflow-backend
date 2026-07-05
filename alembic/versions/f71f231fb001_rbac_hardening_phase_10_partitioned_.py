"""RBAC Hardening Phase 10 - partitioned audit log

Revision ID: f71f231fb001
Revises: fbcddf8779b8
Create Date: 2026-05-23 16:03:41.630268

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f71f231fb001'
down_revision: Union[str, Sequence[str], None] = 'fbcddf8779b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Drop legacy table
    op.execute("DROP TABLE IF EXISTS public.branch_audit_log CASCADE;")
    
    # 1. Create the partitioned table
    op.execute("""
    CREATE TABLE public.branch_audit_log (
        id                UUID NOT NULL DEFAULT gen_random_uuid(),
        audit_sequence    BIGINT GENERATED ALWAYS AS IDENTITY,
        event_id          UUID NOT NULL DEFAULT gen_random_uuid(),
        request_id        UUID NULL,
        branch_id         UUID NOT NULL,
        org_id            UUID NOT NULL,
        region_id         UUID NULL,
        actor_id          UUID NOT NULL,
        actor_snapshot    JSONB NOT NULL,
        actor_permissions JSONB NOT NULL,
        action            VARCHAR(64) NOT NULL,
        action_category   VARCHAR(32) GENERATED ALWAYS AS (split_part(action, '.', 1)) STORED,
        reason_code       VARCHAR(32) NOT NULL,
        reason            TEXT NOT NULL,
        diff              JSONB NULL,
        previous_event_hash VARCHAR(64) NULL,
        event_hash          VARCHAR(64) NOT NULL,
        hash_key_version  SMALLINT NOT NULL DEFAULT 1,
        policy_version    INT NOT NULL DEFAULT 1,
        app_version       VARCHAR(32) NULL,
        deployment_id     UUID NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (id, created_at),
        UNIQUE (event_id, created_at),
        CONSTRAINT chk_prev_hash_chain CHECK (previous_event_hash IS NOT NULL OR action = 'system.bootstrap')
    ) PARTITION BY RANGE (created_at);
    """)

    # Create initial partition for current month to avoid insertion errors
    op.execute("""
    CREATE TABLE public.branch_audit_log_y2026_m05 PARTITION OF public.branch_audit_log
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
    """)

    # Create Indexes
    op.execute("CREATE INDEX ix_audit_org_sequence ON public.branch_audit_log(org_id, audit_sequence DESC);")

    # Roles and Permissions
    # Assuming app_security_owner and audit_writer are already created (or they need to be).
    # Since we can't be sure they exist in the test env, we use DO blocks to safely create/grant.
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'audit_writer') THEN
            CREATE ROLE audit_writer NOLOGIN NOBYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app_security_owner') THEN
            CREATE ROLE app_security_owner NOLOGIN NOINHERIT NOBYPASSRLS;
        END IF;
    END
    $$;
    """)

    op.execute("REVOKE UPDATE, DELETE ON public.branch_audit_log FROM PUBLIC;")
    op.execute("GRANT INSERT, SELECT ON public.branch_audit_log TO audit_writer;")

    # Create the raise_immutable_violation function
    op.execute("""
    CREATE OR REPLACE FUNCTION app_private.raise_immutable_violation()
    RETURNS TRIGGER SECURITY DEFINER SET search_path = pg_catalog AS $$
    BEGIN
        RAISE EXCEPTION 'Audit logs are immutable';
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("ALTER FUNCTION app_private.raise_immutable_violation OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.raise_immutable_violation FROM PUBLIC;")

    # Create Trigger
    op.execute("""
    CREATE TRIGGER trg_deny_audit_mutation
        BEFORE UPDATE OR DELETE ON public.branch_audit_log
        FOR EACH ROW EXECUTE FUNCTION app_private.raise_immutable_violation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_deny_audit_mutation ON public.branch_audit_log;")
    op.execute("DROP FUNCTION IF EXISTS app_private.raise_immutable_violation();")
    op.execute("DROP TABLE IF EXISTS public.branch_audit_log CASCADE;")
