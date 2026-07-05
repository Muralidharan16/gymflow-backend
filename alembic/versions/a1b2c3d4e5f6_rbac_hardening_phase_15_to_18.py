"""RBAC Hardening Phase 15 to 18

Revision ID: a1b2c3d4e5f6
Revises: 970059a0665d
Create Date: 2026-05-23 16:32:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '970059a0665d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 15. RLS Policies
    op.execute("ALTER TABLE public.branch_staff_roles   ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles   FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_members ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_members FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_audit_log     ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_audit_log     FORCE ROW LEVEL SECURITY;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("""
    CREATE POLICY tenant_isolation_staff_roles ON public.branch_staff_roles
    FOR ALL
    USING (
        org_id = current_setting('app.current_org_id', false)::uuid
        AND deleted_at IS NULL
        AND COALESCE(current_setting('app.can_read_staff_roles', true), 'false') = 'true'
    )
    WITH CHECK (
        org_id = current_setting('app.current_org_id', false)::uuid
        AND deleted_at IS NULL
    );
    """)

    # Phase 16. Security Barrier Views
    op.execute("CREATE SCHEMA IF NOT EXISTS app_secure;")
    op.execute("DROP VIEW IF EXISTS app_secure.v_active_branch_staff_roles CASCADE;")
    op.execute("""
    CREATE OR REPLACE VIEW app_secure.v_active_branch_staff_roles
    WITH (security_barrier = true) AS
    SELECT * FROM public.branch_staff_roles
    WHERE deleted_at IS NULL AND revoked_at IS NULL;
    """)

    # Phase 17. Indexes
    op.execute("""
    -- Active role lookups
    CREATE INDEX IF NOT EXISTS ix_roles_active_lookup ON public.branch_staff_roles(org_id, branch_id, organization_member_id)
    WHERE (revoked_at IS NULL AND deleted_at IS NULL);
    """)
    
    op.execute("""
    -- Active member lookups
    CREATE INDEX IF NOT EXISTS ix_member_active ON public.organization_members(org_id, user_id)
    WHERE deleted_at IS NULL;
    """)
    
    op.execute("""
    -- Active snapshot resolution
    CREATE INDEX IF NOT EXISTS ix_snapshot_active ON public.member_permission_snapshots(organization_member_id)
    WHERE is_stale = FALSE;
    """)
    
    op.execute("""
    -- Partition-pruning audit queries
    CREATE INDEX IF NOT EXISTS ix_audit_org_sequence ON public.branch_audit_log(org_id, audit_sequence DESC);
    """)
    
    op.execute("""
    -- Active session queries
    CREATE INDEX IF NOT EXISTS ix_auth_sessions_active ON public.auth_sessions(user_id, org_id)
    WHERE revoked_at IS NULL;
    """)

    # Phase 18. Partition Lifecycle Automation
    op.execute("""
    CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(
        p_table_name TEXT,
        p_days_ahead INT
    )
    RETURNS VOID STRICT VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    DECLARE
        v_qualified_name  TEXT;
        v_partition_date  TIMESTAMPTZ := clock_timestamp() + (p_days_ahead || ' days')::interval;
        v_partition_name  TEXT;
        v_start_str       TEXT;
        v_end_str         TEXT;
    BEGIN
        -- Internal mapping — never interpolate p_table_name directly into DDL
        v_qualified_name := CASE p_table_name
            WHEN 'branch_audit_log' THEN 'public.branch_audit_log'
            WHEN 'auth_sessions'    THEN 'public.auth_sessions'
            ELSE NULL
        END;

        IF v_qualified_name IS NULL THEN
            RAISE EXCEPTION 'Invalid partition target: %', p_table_name;
        END IF;

        v_partition_name := replace(p_table_name, '.', '_') || '_' || to_char(v_partition_date, 'YYYY_MM');
        v_start_str := to_char(date_trunc('month', v_partition_date), 'YYYY-MM-DD');
        v_end_str   := to_char(date_trunc('month', v_partition_date) + interval '1 month', 'YYYY-MM-DD');

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
            v_partition_name, v_qualified_name, v_start_str, v_end_str
        );
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    -- NOTE: OWNER TO app_security_owner might fail if role doesn't exist, we fallback or just omit it if the role isn't guaranteed
    -- For now we just revoke public access.
    REVOKE ALL ON FUNCTION app_private.ensure_future_partition FROM PUBLIC;
    """)


def downgrade() -> None:
    # Phase 18
    op.execute("DROP FUNCTION IF EXISTS app_private.ensure_future_partition;")
    
    # Phase 17
    op.execute("DROP INDEX IF EXISTS ix_auth_sessions_active;")
    op.execute("DROP INDEX IF EXISTS ix_audit_org_sequence;")
    op.execute("DROP INDEX IF EXISTS ix_snapshot_active;")
    op.execute("DROP INDEX IF EXISTS ix_member_active;")
    op.execute("DROP INDEX IF EXISTS ix_roles_active_lookup;")

    # Phase 16
    op.execute("DROP VIEW IF EXISTS app_secure.v_active_branch_staff_roles;")
    
    # Phase 15
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("ALTER TABLE public.branch_audit_log DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_members DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles DISABLE ROW LEVEL SECURITY;")
