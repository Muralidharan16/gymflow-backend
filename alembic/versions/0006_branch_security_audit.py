"""Enterprise Branch Security and Audit

Revision ID: 0006_branch_security_audit
Revises: 0005_enterprise_branches
Create Date: 2026-05-22 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_branch_security_audit"
down_revision: Union[str, None] = "0005_enterprise_branches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _preflight_upgrade() -> None:
    """Require the exact 0005 predecessor and reject object adoption."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
            relation_row record;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'branch_audit_log',
                'branch_audit_log_y2026_m05',
                'idx_audit_log_branch_id_y2026_m05',
                'idx_audit_log_org_id_y2026_m05',
                'outbox_events',
                'outbox_events_y2026_m05',
                'idx_outbox_unprocessed_y2026_m05'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NOT NULL THEN
                    RAISE EXCEPTION
                        '0006 target relation public.% already exists; refusing adoption',
                        relation_name;
                END IF;
            END LOOP;

            IF to_regprocedure(
                'public.create_next_month_partition(text,text[])'
            ) IS NOT NULL THEN
                RAISE EXCEPTION
                    '0006 target function public.create_next_month_partition(text,text[]) already exists; refusing adoption';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_data
                JOIN pg_catalog.pg_class AS relation_data
                  ON relation_data.oid = policy_data.polrelid
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname IN ('org_branches', 'org_branch_state')
                  AND policy_data.polname IN (
                      'tenant_isolation_metadata',
                      'tenant_isolation_state'
                  )
            ) THEN
                RAISE EXCEPTION
                    '0006 predecessor already contains a revision-owned branch RLS policy; refusing adoption';
            END IF;

            FOR relation_row IN
                SELECT relation_data.relname,
                       relation_data.relrowsecurity,
                       relation_data.relforcerowsecurity
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname IN ('org_branches', 'org_branch_state')
            LOOP
                IF relation_row.relrowsecurity
                   OR relation_row.relforcerowsecurity THEN
                    RAISE EXCEPTION
                        '0006 predecessor RLS posture drift on public.%',
                        relation_row.relname;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def _preflight_downgrade() -> None:
    """Fail before mutation if 0005 cannot represent audit/outbox state."""
    op.execute(
        """
        DO $$
        DECLARE
            relation_name text;
            has_rows boolean;
        BEGIN
            FOREACH relation_name IN ARRAY ARRAY[
                'branch_audit_log',
                'outbox_events'
            ] LOOP
                IF to_regclass('public.' || relation_name) IS NULL THEN
                    RAISE EXCEPTION
                        '0006 downgrade drift: required relation public.% is missing',
                        relation_name;
                END IF;

                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)',
                    relation_name
                ) INTO has_rows;

                IF has_rows THEN
                    RAISE EXCEPTION
                        '0006 downgrade would discard populated audit/outbox relation public.%',
                        relation_name;
                END IF;
            END LOOP;

            IF to_regprocedure(
                'public.create_next_month_partition(text,text[])'
            ) IS NULL THEN
                RAISE EXCEPTION
                    '0006 downgrade drift: revision-owned partition helper is missing';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_data
                JOIN pg_catalog.pg_class AS relation_data
                  ON relation_data.oid = policy_data.polrelid
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = 'branch_audit_log'
                  AND policy_data.polname = 'tenant_isolation_audit'
            ) THEN
                RAISE EXCEPTION
                    '0006 downgrade drift: tenant_isolation_audit policy is missing';
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight_upgrade()

    # --- Ticket 3: Tenancy Layer (RLS) ---
    op.execute("ALTER TABLE org_branches ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE org_branch_state ENABLE ROW LEVEL SECURITY;")

    # Custom PostgreSQL GUC placeholders can become the empty string after a
    # transaction-local setting is reset (for example across Alembic autocommit
    # boundaries). Treat both missing and empty tenant context as NULL so the
    # policy fails closed instead of raising an invalid UUID cast.
    op.execute("""
        CREATE POLICY tenant_isolation_metadata ON org_branches
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
        );
    """)
    op.execute("""
        CREATE POLICY tenant_isolation_state ON org_branch_state
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
        );
    """)

    # --- Ticket 4: Audit Log + Partition Automation ---

    # 1. Create branch_audit_log partitioned table
    op.execute("""
        CREATE TABLE branch_audit_log (
            id UUID DEFAULT gen_random_uuid(),
            branch_id UUID NOT NULL,
            org_id UUID NOT NULL,
            actor_id UUID NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NULL,
            diff JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at),
            CONSTRAINT fk_audit_branch FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id),
            CONSTRAINT chk_reason_on_destructive CHECK (
              action NOT IN ('soft_deleted', 'archived', 'purged') OR
              (reason IS NOT NULL AND length(trim(reason)) >= 5)
            )
        ) PARTITION BY RANGE (created_at);
    """)

    # 2. Bootstrap partition y2026_m05
    op.execute("""
        CREATE TABLE branch_audit_log_y2026_m05 PARTITION OF branch_audit_log
        FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
    """)

    # 3. Create indexes on bootstrap partition
    op.execute("""
        CREATE INDEX idx_audit_log_branch_id_y2026_m05 ON branch_audit_log_y2026_m05(branch_id, created_at DESC);
    """)
    op.execute("""
        CREATE INDEX idx_audit_log_org_id_y2026_m05 ON branch_audit_log_y2026_m05(org_id, created_at DESC);
    """)

    # 4. Enable RLS on audit log
    op.execute("ALTER TABLE branch_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation_audit ON branch_audit_log
        USING (
            org_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::UUID
        );
    """)

    # 5. Create dynamic partition automation function. This revision is the
    # sole owner of the helper, so collisions are rejected by preflight rather
    # than overwritten with CREATE OR REPLACE.
    op.execute("""
        CREATE FUNCTION create_next_month_partition(
          table_name TEXT,
          index_ddls TEXT[] DEFAULT ARRAY[]::TEXT[]
        ) RETURNS void AS $$
        DECLARE
          next_month DATE := date_trunc('month', now()) + interval '1 month';
          partition_name TEXT;
          start_val TEXT;
          end_val TEXT;
          idx_ddl TEXT;
        BEGIN
          partition_name := table_name || '_y' || to_char(next_month, 'YYYY') || '_m' || to_char(next_month, 'MM');
          start_val := to_char(next_month, 'YYYY-MM-01');
          end_val := to_char(next_month + interval '1 month', 'YYYY-MM-01');

          EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            partition_name, table_name, start_val, end_val
          );

          FOREACH idx_ddl IN ARRAY index_ddls
          LOOP
            EXECUTE replace(idx_ddl, '__PARTITION_NAME__', partition_name);
          END LOOP;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # --- Ticket 5: Outbox Hardening ---
    op.execute("""
        CREATE TABLE outbox_events (
            event_id UUID NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id UUID NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ NULL,
            locked_at TIMESTAMPTZ NULL,
            locked_by TEXT NULL,
            lease_fencing_token BIGINT NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ NULL,
            failed_at TIMESTAMPTZ NULL,
            failure_reason TEXT NULL,
            PRIMARY KEY (event_id, created_at)
        ) PARTITION BY RANGE (created_at);
    """)

    # Bootstrap partition for outbox events
    op.execute("""
        CREATE TABLE outbox_events_y2026_m05 PARTITION OF outbox_events
        FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
    """)

    # Index for outbox
    op.execute("""
        CREATE INDEX idx_outbox_unprocessed_y2026_m05 ON outbox_events_y2026_m05(created_at) WHERE processed_at IS NULL;
    """)


def downgrade() -> None:
    _preflight_downgrade()

    # --- Ticket 5 Reversals ---
    op.execute("DROP TABLE public.outbox_events RESTRICT;")

    # --- Ticket 4 Reversals ---
    op.execute("DROP FUNCTION public.create_next_month_partition(TEXT, TEXT[]) RESTRICT;")
    op.execute("DROP POLICY tenant_isolation_audit ON branch_audit_log;")
    op.execute("ALTER TABLE branch_audit_log DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP TABLE public.branch_audit_log RESTRICT;")

    # --- Ticket 3 Reversals ---
    op.execute("DROP POLICY tenant_isolation_state ON org_branch_state;")
    op.execute("DROP POLICY tenant_isolation_metadata ON org_branches;")
    op.execute("ALTER TABLE org_branch_state DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE org_branches DISABLE ROW LEVEL SECURITY;")
