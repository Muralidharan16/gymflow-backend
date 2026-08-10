"""branch_lifecycle_control_plane

Revision ID: df59095a360e
Revises: dbeb400472ec
Create Date: 2026-05-23 21:30:19.130288

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df59095a360e'
down_revision: Union[str, Sequence[str], None] = 'dbeb400472ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Auth Schema (for RLS)
    op.execute("CREATE SCHEMA IF NOT EXISTS auth;")
    op.execute("""
    CREATE OR REPLACE FUNCTION auth.role() RETURNS TEXT LANGUAGE SQL STABLE AS $$
        SELECT NULLIF(current_setting('app.current_role', true), '');
    $$;
    """)

    # 1. Schema: Reference Tables
    op.execute("""
    CREATE TABLE public.branch_status_definitions (
        code TEXT PRIMARY KEY,
        is_operational BOOLEAN NOT NULL,
        is_terminal BOOLEAN NOT NULL,
        system_health_state TEXT NOT NULL CHECK (
            system_health_state IN ('healthy', 'degraded', 'read_only', 'maintenance', 'frozen')
        ),
        display_order INT NOT NULL
    );
    """)

    op.execute("""
    CREATE TABLE public.branch_status_transitions (
        from_status TEXT NOT NULL REFERENCES public.branch_status_definitions(code),
        to_status   TEXT NOT NULL REFERENCES public.branch_status_definitions(code),
        allowed_roles TEXT[] NOT NULL,
        requires_reason BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (from_status, to_status)
    );
    """)

    op.execute("""
    CREATE TABLE public.branch_deactivation_policies (
        from_status TEXT NOT NULL REFERENCES public.branch_status_definitions(code),
        to_status   TEXT NOT NULL REFERENCES public.branch_status_definitions(code),
        booking_grace_hours  INT NOT NULL DEFAULT 24,
        auto_cancel_bookings BOOLEAN NOT NULL DEFAULT FALSE,
        notify_members       BOOLEAN NOT NULL DEFAULT TRUE,
        refund_policy TEXT NOT NULL CHECK (
            refund_policy IN ('full', 'credit_only', 'none', 'prorated')
        ),
        PRIMARY KEY (from_status, to_status),
        FOREIGN KEY (from_status, to_status)
            REFERENCES public.branch_status_transitions(from_status, to_status)
    );
    """)

    # 2. Schema: Core State Table Additions
    op.execute("""
    ALTER TABLE public.org_branch_state
        ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
            REFERENCES public.branch_status_definitions(code),
        ADD COLUMN is_operational BOOLEAN NOT NULL DEFAULT TRUE,
        ADD COLUMN status_changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        ADD COLUMN status_changed_by UUID REFERENCES public.organization_users(id) ON DELETE SET NULL,
        ADD COLUMN status_reason TEXT,
        ADD COLUMN transition_source VARCHAR(50) NOT NULL DEFAULT 'api',
        ADD COLUMN scheduled_transition_at TIMESTAMPTZ,
        ADD COLUMN scheduled_transition_to TEXT REFERENCES public.branch_status_definitions(code),
        ADD COLUMN lifecycle_transition_in_progress BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN saga_last_checkpoint TEXT,
        ADD COLUMN saga_compensation_strategy TEXT CHECK (
            saga_compensation_strategy IN ('rollback_to_origin', 'advance_to_target', 'manual_review')
        ),
        ADD CONSTRAINT chk_saga_last_checkpoint CHECK (
            saga_last_checkpoint IS NULL OR saga_last_checkpoint IN (
                'search_deindexed', 'bookings_cancelled',
                'refunds_initiated', 'refunds_completed',
                'notifications_sent',
                'compensation_initiated', 'compensation_completed'
            )
        ),
        ADD COLUMN watchdog_recovered_at TIMESTAMPTZ,
        ADD COLUMN watchdog_recovery_count INT NOT NULL DEFAULT 0,
        ADD COLUMN search_visibility_version BIGINT NOT NULL DEFAULT 1,
        ADD COLUMN search_last_synced_at TIMESTAMPTZ,
        ADD COLUMN search_sync_failed_at TIMESTAMPTZ,
        ADD COLUMN reconciliation_claimed_by UUID,
        ADD COLUMN reconciliation_claimed_at TIMESTAMPTZ,
        ADD COLUMN worm_archive_uri TEXT,
        ADD COLUMN worm_archive_checksum TEXT,
        ADD COLUMN worm_archive_verified_at TIMESTAMPTZ,
        ADD COLUMN worm_archive_status TEXT CHECK (
            worm_archive_status IN ('pending', 'written', 'verified', 'failed')
        );
    """)
    op.execute("ALTER TABLE public.org_branch_state ALTER COLUMN transition_source DROP DEFAULT;")

    # Check Constraints on Core State Table
    op.execute("""
    ALTER TABLE public.org_branch_state
        ADD CONSTRAINT chk_transition_source CHECK (transition_source IN (
            'api', 'scheduled_job', 'admin_panel',
            'compliance_trigger', 'system_watchdog', 'saga_compensation'
        )),
        ADD CONSTRAINT chk_terminal_status_reason CHECK (
            NOT (status IN ('permanently_closed', 'compliance_suspended'))
            OR (status_reason IS NOT NULL AND trim(status_reason) <> '')
        ),
        ADD CONSTRAINT chk_scheduled_transition_pair CHECK (
            (scheduled_transition_at IS NULL) = (scheduled_transition_to IS NULL)
        ),
        ADD CONSTRAINT chk_no_delete_while_operational CHECK (
            deleted_at IS NULL
            OR (is_operational = FALSE AND lifecycle_transition_in_progress = FALSE)
        );
    """)

    # Indexes
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_branch_operational_lookup
        ON public.org_branch_state (org_id, status, is_operational)
        WHERE deleted_at IS NULL;
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_branch_public_discovery
        ON public.org_branch_state (is_public, status)
        WHERE status = 'active';
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_branch_reconciliation_candidates
        ON public.org_branch_state (search_last_synced_at, reconciliation_claimed_at)
        WHERE deleted_at IS NULL;
    """)

    # 3. Triggers: Core State Table
    op.execute("""
    CREATE OR REPLACE FUNCTION sync_branch_operational_state()
    RETURNS trigger LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
    BEGIN
        SELECT is_operational INTO NEW.is_operational
        FROM public.branch_status_definitions WHERE code = NEW.status;
        RETURN NEW;
    END;
    $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_sync_operational_state
    BEFORE INSERT OR UPDATE OF status ON public.org_branch_state
    FOR EACH ROW EXECUTE FUNCTION sync_branch_operational_state();
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION validate_scheduled_transition()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.scheduled_transition_at IS NOT NULL
           AND NEW.scheduled_transition_at <= clock_timestamp()
           AND (OLD.scheduled_transition_at IS DISTINCT FROM NEW.scheduled_transition_at)
        THEN
            RAISE EXCEPTION 'scheduled_transition_at must be a future timestamp';
        END IF;
        RETURN NEW;
    END;
    $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_validate_scheduled_transition
    BEFORE INSERT OR UPDATE OF scheduled_transition_at ON public.org_branch_state
    FOR EACH ROW EXECUTE FUNCTION validate_scheduled_transition();
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION guard_worm_immutability()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.worm_archive_status = 'verified' AND (
            NEW.worm_archive_uri      IS DISTINCT FROM OLD.worm_archive_uri      OR
            NEW.worm_archive_checksum IS DISTINCT FROM OLD.worm_archive_checksum OR
            NEW.worm_archive_status   IS DISTINCT FROM OLD.worm_archive_status
        ) THEN
            RAISE EXCEPTION
                'WORM archive is verified and immutable. Branch: %', OLD.branch_id;
        END IF;
        RETURN NEW;
    END;
    $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_worm_immutability
    BEFORE UPDATE OF worm_archive_uri, worm_archive_checksum, worm_archive_status
    ON public.org_branch_state
    FOR EACH ROW EXECUTE FUNCTION guard_worm_immutability();
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION enforce_branch_transition_freeze()
    RETURNS trigger LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
    DECLARE
        v_in_progress BOOLEAN;
    BEGIN
        SELECT lifecycle_transition_in_progress INTO v_in_progress
        FROM public.org_branch_state
        WHERE branch_id = NEW.branch_id
        FOR SHARE;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'Branch % not found in org_branch_state. Write rejected.', NEW.branch_id;
        END IF;

        IF v_in_progress = TRUE THEN
            RAISE EXCEPTION
                'Writes rejected: Branch % is under lifecycle freeze.', NEW.branch_id;
        END IF;
        RETURN NEW;
    END;
    $$;
    """)
    op.execute("""
    DO $$ 
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'bookings') THEN
            EXECUTE 'CREATE TRIGGER trg_freeze_guard_bookings BEFORE INSERT OR UPDATE ON public.bookings FOR EACH ROW EXECUTE FUNCTION enforce_branch_transition_freeze()';
        END IF;
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'schedules') THEN
            EXECUTE 'CREATE TRIGGER trg_freeze_guard_schedules BEFORE INSERT OR UPDATE ON public.schedules FOR EACH ROW EXECUTE FUNCTION enforce_branch_transition_freeze()';
        END IF;
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'trainer_assignments') THEN
            EXECUTE 'CREATE TRIGGER trg_freeze_guard_trainer_assignments BEFORE INSERT OR UPDATE ON public.trainer_assignments FOR EACH ROW EXECUTE FUNCTION enforce_branch_transition_freeze()';
        END IF;
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'memberships') THEN
            EXECUTE 'CREATE TRIGGER trg_freeze_guard_memberships BEFORE INSERT OR UPDATE ON public.memberships FOR EACH ROW EXECUTE FUNCTION enforce_branch_transition_freeze()';
        END IF;
    END $$;
    """)

    # 4 & 5. Audit History Ledger & RLS
    op.execute("""
    CREATE TABLE public.branch_status_history (
        history_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        branch_id    UUID NOT NULL REFERENCES public.org_branches(id),
        from_status  TEXT REFERENCES public.branch_status_definitions(code),
        to_status    TEXT NOT NULL REFERENCES public.branch_status_definitions(code),
        changed_by   UUID REFERENCES public.organization_users(id) ON DELETE SET NULL,
        changed_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        reason       TEXT,
        transition_source VARCHAR(50),
        snapshot     JSONB NOT NULL,
        correlation_id UUID,
        correlation_emitted_at TIMESTAMPTZ,
        CONSTRAINT chk_history_transition_source CHECK (
            transition_source IS NULL OR transition_source IN (
                'api', 'scheduled_job', 'admin_panel',
                'compliance_trigger', 'system_watchdog', 'saga_compensation'
            )
        )
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_branch_history_lookup
        ON public.branch_status_history (branch_id, changed_at DESC);
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION prevent_history_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION
            'branch_status_history is append-only. % is forbidden.', TG_OP;
        RETURN NULL;
    END;
    $$;
    """)
    op.execute("""
    CREATE TRIGGER trg_history_append_only
    BEFORE UPDATE OR DELETE ON public.branch_status_history
    FOR EACH ROW EXECUTE FUNCTION prevent_history_mutation();
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION validate_history_correlation()
    RETURNS trigger LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
    BEGIN
        IF NEW.correlation_id IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.branch_lifecycle_events
                WHERE correlation_id = NEW.correlation_id
                AND emitted_at >= COALESCE(
                    NEW.correlation_emitted_at,
                    NOW() - INTERVAL '24 hours'
                )
                LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'correlation_id % not found in branch_lifecycle_events', NEW.correlation_id;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$;
    """)

    # 6. Canonical Event Store
    op.execute("""
    CREATE TABLE public.branch_lifecycle_events (
        event_id      UUID DEFAULT gen_random_uuid(),
        branch_id     UUID NOT NULL REFERENCES public.org_branches(id),
        event_type    VARCHAR(100) NOT NULL,
        event_version INT NOT NULL DEFAULT 1,
        payload       JSONB NOT NULL,
        emitted_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        correlation_id UUID NOT NULL,
        step_sequence INT NOT NULL DEFAULT 1,
        PRIMARY KEY (event_id, emitted_at)
    ) PARTITION BY RANGE (emitted_at);
    """)
    op.execute("""
    ALTER TABLE public.branch_lifecycle_events
        ADD CONSTRAINT uq_saga_event_step UNIQUE (correlation_id, event_type, step_sequence, emitted_at);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_lifecycle_events_correlation
        ON public.branch_lifecycle_events (correlation_id, step_sequence);
    """)
    op.execute("""
    CREATE TABLE public.branch_lifecycle_events_2026_q2
        PARTITION OF public.branch_lifecycle_events
        FOR VALUES FROM ('2026-04-01') TO ('2026-07-01');
    """)
    op.execute("""
    CREATE TABLE public.branch_lifecycle_events_2026_q3
        PARTITION OF public.branch_lifecycle_events
        FOR VALUES FROM ('2026-07-01') TO ('2026-10-01');
    """)

    # Now that branch_lifecycle_events exists, we can attach the trigger to history
    op.execute("""
    CREATE TRIGGER trg_validate_history_correlation
    BEFORE INSERT ON public.branch_status_history
    FOR EACH ROW EXECUTE FUNCTION validate_history_correlation();
    """)

    # 7. Transactional Outbox
    op.execute("""
    CREATE TABLE public.branch_outbox_events (
        outbox_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        branch_id     UUID NOT NULL REFERENCES public.org_branches(id),
        event_type    VARCHAR(100) NOT NULL,
        payload       JSONB NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        process_after TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                          'pending', 'processing', 'delivered',
                          'dead_lettered', 'quarantined', 'compatibility_queue'
                      )),
        attempt_count INT NOT NULL DEFAULT 0,
        max_attempts  INT NOT NULL DEFAULT 5,
        last_attempted_at TIMESTAMPTZ,
        last_error    TEXT,
        correlation_id UUID NOT NULL
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_outbox_pending
        ON public.branch_outbox_events (process_after, created_at)
        WHERE status = 'pending';
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_outbox_stuck_processing
        ON public.branch_outbox_events (last_attempted_at)
        WHERE status = 'processing';
    """)

    # 9. Watchdog Alerting Contract
    op.execute("""
    CREATE TABLE public.branch_watchdog_alerts (
        alert_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        branch_id   UUID NOT NULL REFERENCES public.org_branches(id),
        alert_type  TEXT NOT NULL CHECK (alert_type IN (
            'freeze_threshold_15m',
            'force_recovery_45m',
            'worm_write_failed',
            'partition_gap_detected',
            'reconciliation_stale'
        )),
        triggered_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        resolved_at       TIMESTAMPTZ,
        resolution_notes  TEXT
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_watchdog_open_alerts
        ON public.branch_watchdog_alerts (branch_id, triggered_at)
        WHERE resolved_at IS NULL;
    """)

    # 4. RLS & Policy Matrix
    op.execute("ALTER TABLE public.org_branch_state ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_status_history ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_lifecycle_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_outbox_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_watchdog_alerts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.org_branch_state FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_status_history FORCE ROW LEVEL SECURITY;")

    # Policies
    op.execute("""
    CREATE POLICY p_branch_select ON public.org_branch_state FOR SELECT USING (
        org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID AND (
            (auth.role() IN ('manager', 'trainer') AND is_operational = TRUE) OR
            (auth.role() IN ('owner', 'org_admin') AND status != 'permanently_closed') OR
            auth.role() IN ('compliance', 'superadmin')
        )
    );
    """)
    op.execute("""
    CREATE POLICY p_branch_update ON public.org_branch_state FOR UPDATE USING (
        (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
         AND auth.role() IN ('owner', 'org_admin', 'compliance', 'superadmin'))
        OR auth.role() IN ('system', 'saga_orchestrator', 'system_watchdog')
    );
    """)
    op.execute("""
    CREATE POLICY p_branch_insert ON public.org_branch_state FOR INSERT WITH CHECK (
        auth.role() IN ('superadmin', 'system')
    );
    """)
    op.execute("""
    CREATE POLICY p_branch_delete ON public.org_branch_state FOR DELETE USING (
        auth.role() = 'superadmin'
    );
    """)

    op.execute("""
    CREATE POLICY p_history_select ON public.branch_status_history FOR SELECT USING (
        auth.role() IN ('owner', 'org_admin', 'compliance', 'superadmin')
    );
    """)

    op.execute("""
    CREATE POLICY p_outbox_insert ON public.branch_outbox_events
    FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'));
    """)
    op.execute("""
    CREATE POLICY p_outbox_update ON public.branch_outbox_events
    FOR UPDATE USING (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'));
    """)
    op.execute("""
    CREATE POLICY p_outbox_select ON public.branch_outbox_events
    FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'));
    """)

    op.execute("""
    CREATE POLICY p_events_insert ON public.branch_lifecycle_events
    FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'));
    """)
    op.execute("""
    CREATE POLICY p_events_select ON public.branch_lifecycle_events
    FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'));
    """)

    op.execute("""
    CREATE POLICY p_watchdog_insert ON public.branch_watchdog_alerts
    FOR INSERT WITH CHECK (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'));
    """)
    op.execute("""
    CREATE POLICY p_watchdog_update ON public.branch_watchdog_alerts
    FOR UPDATE USING (auth.role() IN ('system', 'superadmin', 'saga_orchestrator'));
    """)
    op.execute("""
    CREATE POLICY p_watchdog_select ON public.branch_watchdog_alerts
    FOR SELECT USING (auth.role() IN ('superadmin', 'compliance', 'system'));
    """)

    # 11. Canonical Seed Data
    op.execute("""
    INSERT INTO public.branch_status_definitions
        (code, is_operational, is_terminal, system_health_state, display_order) VALUES
    ('active',               TRUE,  FALSE, 'healthy',     1),
    ('temporarily_closed',   FALSE, FALSE, 'maintenance', 2),
    ('under_renovation',     FALSE, FALSE, 'maintenance', 3),
    ('compliance_suspended', FALSE, FALSE, 'frozen',      4),
    ('permanently_closed',   FALSE, TRUE,  'frozen',      5)
    ON CONFLICT (code) DO NOTHING;
    """)

    op.execute("""
    INSERT INTO public.branch_status_transitions
        (from_status, to_status, allowed_roles, requires_reason) VALUES
    ('active',               'temporarily_closed',   ARRAY['owner','org_admin'],       FALSE),
    ('active',               'under_renovation',     ARRAY['owner','org_admin'],       FALSE),
    ('active',               'compliance_suspended', ARRAY['compliance','superadmin'], TRUE),
    ('active',               'permanently_closed',   ARRAY['owner','superadmin'],      TRUE),
    ('temporarily_closed',   'active',               ARRAY['owner','org_admin'],       FALSE),
    ('temporarily_closed',   'permanently_closed',    ARRAY['owner','superadmin'],      TRUE),
    ('under_renovation',     'active',               ARRAY['owner','org_admin'],       FALSE),
    ('compliance_suspended', 'active',               ARRAY['compliance','superadmin'], TRUE),
    ('compliance_suspended', 'permanently_closed',   ARRAY['compliance','superadmin'], TRUE)
    ON CONFLICT DO NOTHING;
    """)

    op.execute("""
    INSERT INTO public.branch_deactivation_policies
        (from_status, to_status, booking_grace_hours, auto_cancel_bookings, notify_members, refund_policy)
    VALUES
    ('active',               'temporarily_closed',    48, FALSE, TRUE,  'full'),
    ('active',               'under_renovation',      72, FALSE, TRUE,  'full'),
    ('active',               'compliance_suspended',   0, TRUE,  TRUE,  'full'),
    ('active',               'permanently_closed',    24, TRUE,  TRUE,  'full'),
    ('temporarily_closed',   'active',                 0, FALSE, FALSE, 'none'),
    ('temporarily_closed',   'permanently_closed',    24, TRUE,  TRUE,  'full'),
    ('under_renovation',     'active',                 0, FALSE, FALSE, 'none'),
    ('compliance_suspended', 'active',                 0, FALSE, TRUE,  'none'),
    ('compliance_suspended', 'permanently_closed',     0, TRUE,  TRUE,  'full')
    ON CONFLICT DO NOTHING;
    """)


def downgrade() -> None:
    """Restore the exact DBEB predecessor surface without cascading drift."""
    # Fail before mutation if the lifecycle surface is already incomplete or if
    # Alembic is not running under the reduced migration identity.
    op.execute("""
    DO $$
    DECLARE
        relation_name text;
        function_name text;
        required_column_count integer;
        required_policy_count integer;
        required_trigger_count integer;
        state_record record;
    BEGIN
        IF session_user <> 'migration_owner' OR current_user <> 'migration_owner' THEN
            RAISE EXCEPTION
                'DF590 downgrade requires session_user=current_user=migration_owner';
        END IF;

        FOREACH relation_name IN ARRAY ARRAY[
            'public.org_branch_state',
            'public.branch_status_definitions',
            'public.branch_status_transitions',
            'public.branch_deactivation_policies',
            'public.branch_status_history',
            'public.branch_lifecycle_events',
            'public.branch_lifecycle_events_2026_q2',
            'public.branch_lifecycle_events_2026_q3',
            'public.branch_outbox_events',
            'public.branch_watchdog_alerts'
        ]
        LOOP
            IF pg_catalog.to_regclass(relation_name) IS NULL THEN
                RAISE EXCEPTION 'DF590 required relation is absent before downgrade: %', relation_name;
            END IF;
        END LOOP;

        SELECT count(*) INTO required_column_count
        FROM pg_catalog.pg_attribute AS attribute_data
        WHERE attribute_data.attrelid = 'public.org_branch_state'::regclass
          AND attribute_data.attnum > 0
          AND NOT attribute_data.attisdropped
          AND attribute_data.attname = ANY (ARRAY[
              'status', 'is_operational', 'status_changed_at', 'status_changed_by',
              'status_reason', 'transition_source', 'scheduled_transition_at',
              'scheduled_transition_to', 'lifecycle_transition_in_progress',
              'saga_last_checkpoint', 'saga_compensation_strategy',
              'watchdog_recovered_at', 'watchdog_recovery_count',
              'search_visibility_version', 'search_last_synced_at',
              'search_sync_failed_at', 'reconciliation_claimed_by',
              'reconciliation_claimed_at', 'worm_archive_uri',
              'worm_archive_checksum', 'worm_archive_verified_at',
              'worm_archive_status'
          ]);
        IF required_column_count <> 22 THEN
            RAISE EXCEPTION
                'DF590 org_branch_state column surface drifted: expected 22, observed %',
                required_column_count;
        END IF;

        SELECT count(*) INTO required_policy_count
        FROM pg_catalog.pg_policy AS policy_data
        WHERE policy_data.polrelid = 'public.org_branch_state'::regclass
          AND policy_data.polname = ANY (ARRAY[
              'p_branch_select', 'p_branch_update',
              'p_branch_insert', 'p_branch_delete'
          ]);
        IF required_policy_count <> 4 THEN
            RAISE EXCEPTION
                'DF590 org_branch_state policy surface drifted: expected 4, observed %',
                required_policy_count;
        END IF;

        SELECT count(*) INTO required_trigger_count
        FROM pg_catalog.pg_trigger AS trigger_data
        WHERE trigger_data.tgrelid = 'public.org_branch_state'::regclass
          AND NOT trigger_data.tgisinternal
          AND trigger_data.tgname = ANY (ARRAY[
              'trg_sync_operational_state',
              'trg_validate_scheduled_transition',
              'trg_guard_worm_immutability'
          ]);
        IF required_trigger_count <> 3 THEN
            RAISE EXCEPTION
                'DF590 org_branch_state trigger surface drifted: expected 3, observed %',
                required_trigger_count;
        END IF;

        FOREACH function_name IN ARRAY ARRAY[
            'auth.role()',
            'public.sync_branch_operational_state()',
            'public.validate_scheduled_transition()',
            'public.guard_worm_immutability()',
            'public.enforce_branch_transition_freeze()',
            'public.prevent_history_mutation()',
            'public.validate_history_correlation()'
        ]
        LOOP
            IF pg_catalog.to_regprocedure(function_name) IS NULL THEN
                RAISE EXCEPTION 'DF590 required function is absent before downgrade: %', function_name;
            END IF;
        END LOOP;

        SELECT relrowsecurity, relforcerowsecurity
        INTO state_record
        FROM pg_catalog.pg_class
        WHERE oid = 'public.org_branch_state'::regclass;
        IF NOT state_record.relrowsecurity OR NOT state_record.relforcerowsecurity THEN
            RAISE EXCEPTION
                'DF590 predecessor-owned org_branch_state lost its required RLS posture';
        END IF;
    END
    $$;
    """)

    # Policies attached to the predecessor-owned state table are DF590-owned.
    op.execute("DROP POLICY p_branch_select ON public.org_branch_state;")
    op.execute("DROP POLICY p_branch_update ON public.org_branch_state;")
    op.execute("DROP POLICY p_branch_insert ON public.org_branch_state;")
    op.execute("DROP POLICY p_branch_delete ON public.org_branch_state;")

    # Optional freeze guards were installed only when predecessor relations were
    # present. Detach them conditionally, but never suppress an unexpected error
    # on an existing relation.
    op.execute("""
    DO $$
    BEGIN
        IF pg_catalog.to_regclass('public.bookings') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS trg_freeze_guard_bookings ON public.bookings;
        END IF;
        IF pg_catalog.to_regclass('public.schedules') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS trg_freeze_guard_schedules ON public.schedules;
        END IF;
        IF pg_catalog.to_regclass('public.trainer_assignments') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS trg_freeze_guard_trainer_assignments ON public.trainer_assignments;
        END IF;
        IF pg_catalog.to_regclass('public.memberships') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS trg_freeze_guard_memberships ON public.memberships;
        END IF;
    END
    $$;
    """)

    op.execute("DROP TRIGGER trg_sync_operational_state ON public.org_branch_state;")
    op.execute("DROP TRIGGER trg_validate_scheduled_transition ON public.org_branch_state;")
    op.execute("DROP TRIGGER trg_guard_worm_immutability ON public.org_branch_state;")
    op.execute("DROP TRIGGER trg_validate_history_correlation ON public.branch_status_history;")
    op.execute("DROP TRIGGER trg_history_append_only ON public.branch_status_history;")

    # Remove DF590-owned relations in dependency order. No CASCADE is used: an
    # unknown external dependency is production drift and must block rollback.
    op.execute("DROP TABLE public.branch_status_history;")
    op.execute("DROP TABLE public.branch_lifecycle_events_2026_q2;")
    op.execute("DROP TABLE public.branch_lifecycle_events_2026_q3;")
    op.execute("DROP TABLE public.branch_lifecycle_events;")
    op.execute("DROP TABLE public.branch_outbox_events;")
    op.execute("DROP TABLE public.branch_watchdog_alerts;")

    # Trigger functions are now unreferenced by DF590-owned objects. RESTRICT is
    # intentional so another object's dependency cannot be silently destroyed.
    op.execute("DROP FUNCTION public.validate_history_correlation();")
    op.execute("DROP FUNCTION public.prevent_history_mutation();")
    op.execute("DROP FUNCTION public.sync_branch_operational_state();")
    op.execute("DROP FUNCTION public.validate_scheduled_transition();")
    op.execute("DROP FUNCTION public.guard_worm_immutability();")
    op.execute("DROP FUNCTION public.enforce_branch_transition_freeze();")

    op.execute("DROP INDEX public.ix_branch_operational_lookup;")
    op.execute("DROP INDEX public.ix_branch_public_discovery;")
    op.execute("DROP INDEX public.ix_branch_reconciliation_candidates;")

    op.execute("""
    ALTER TABLE public.org_branch_state
        DROP CONSTRAINT chk_transition_source,
        DROP CONSTRAINT chk_terminal_status_reason,
        DROP CONSTRAINT chk_scheduled_transition_pair,
        DROP CONSTRAINT chk_no_delete_while_operational,
        DROP CONSTRAINT chk_saga_last_checkpoint;
    """)

    op.execute("""
    ALTER TABLE public.org_branch_state
        DROP COLUMN status,
        DROP COLUMN is_operational,
        DROP COLUMN status_changed_at,
        DROP COLUMN status_changed_by,
        DROP COLUMN status_reason,
        DROP COLUMN transition_source,
        DROP COLUMN scheduled_transition_at,
        DROP COLUMN scheduled_transition_to,
        DROP COLUMN lifecycle_transition_in_progress,
        DROP COLUMN saga_last_checkpoint,
        DROP COLUMN saga_compensation_strategy,
        DROP COLUMN watchdog_recovered_at,
        DROP COLUMN watchdog_recovery_count,
        DROP COLUMN search_visibility_version,
        DROP COLUMN search_last_synced_at,
        DROP COLUMN search_sync_failed_at,
        DROP COLUMN reconciliation_claimed_by,
        DROP COLUMN reconciliation_claimed_at,
        DROP COLUMN worm_archive_uri,
        DROP COLUMN worm_archive_checksum,
        DROP COLUMN worm_archive_verified_at,
        DROP COLUMN worm_archive_status;
    """)

    op.execute("DROP TABLE public.branch_deactivation_policies;")
    op.execute("DROP TABLE public.branch_status_transitions;")
    op.execute("DROP TABLE public.branch_status_definitions;")

    # auth is revision-owned. DROP SCHEMA is deliberately RESTRICT (the default)
    # so any leaked successor object becomes a hard lifecycle failure.
    op.execute("DROP FUNCTION auth.role();")
    op.execute("DROP SCHEMA auth;")

    # Prove that only DF590-owned state was removed and predecessor security was
    # preserved. This blocks a nominally successful but semantically partial
    # downgrade.
    op.execute("""
    DO $$
    DECLARE
        relation_name text;
        function_name text;
        residual_column_count integer;
        residual_policy_count integer;
        state_record record;
    BEGIN
        FOREACH relation_name IN ARRAY ARRAY[
            'public.branch_status_definitions',
            'public.branch_status_transitions',
            'public.branch_deactivation_policies',
            'public.branch_status_history',
            'public.branch_lifecycle_events',
            'public.branch_lifecycle_events_2026_q2',
            'public.branch_lifecycle_events_2026_q3',
            'public.branch_outbox_events',
            'public.branch_watchdog_alerts'
        ]
        LOOP
            IF pg_catalog.to_regclass(relation_name) IS NOT NULL THEN
                RAISE EXCEPTION 'DF590 relation remained after downgrade: %', relation_name;
            END IF;
        END LOOP;

        SELECT count(*) INTO residual_column_count
        FROM pg_catalog.pg_attribute AS attribute_data
        WHERE attribute_data.attrelid = 'public.org_branch_state'::regclass
          AND attribute_data.attnum > 0
          AND NOT attribute_data.attisdropped
          AND attribute_data.attname = ANY (ARRAY[
              'status', 'is_operational', 'status_changed_at', 'status_changed_by',
              'status_reason', 'transition_source', 'scheduled_transition_at',
              'scheduled_transition_to', 'lifecycle_transition_in_progress',
              'saga_last_checkpoint', 'saga_compensation_strategy',
              'watchdog_recovered_at', 'watchdog_recovery_count',
              'search_visibility_version', 'search_last_synced_at',
              'search_sync_failed_at', 'reconciliation_claimed_by',
              'reconciliation_claimed_at', 'worm_archive_uri',
              'worm_archive_checksum', 'worm_archive_verified_at',
              'worm_archive_status'
          ]);
        IF residual_column_count <> 0 THEN
            RAISE EXCEPTION
                'DF590 org_branch_state columns remained after downgrade: %',
                residual_column_count;
        END IF;

        SELECT count(*) INTO residual_policy_count
        FROM pg_catalog.pg_policy AS policy_data
        WHERE policy_data.polrelid = 'public.org_branch_state'::regclass
          AND policy_data.polname = ANY (ARRAY[
              'p_branch_select', 'p_branch_update',
              'p_branch_insert', 'p_branch_delete'
          ]);
        IF residual_policy_count <> 0 THEN
            RAISE EXCEPTION
                'DF590 org_branch_state policies remained after downgrade: %',
                residual_policy_count;
        END IF;

        FOREACH function_name IN ARRAY ARRAY[
            'public.sync_branch_operational_state()',
            'public.validate_scheduled_transition()',
            'public.guard_worm_immutability()',
            'public.enforce_branch_transition_freeze()',
            'public.prevent_history_mutation()',
            'public.validate_history_correlation()'
        ]
        LOOP
            IF pg_catalog.to_regprocedure(function_name) IS NOT NULL THEN
                RAISE EXCEPTION 'DF590 function remained after downgrade: %', function_name;
            END IF;
        END LOOP;

        IF pg_catalog.to_regnamespace('auth') IS NOT NULL THEN
            RAISE EXCEPTION 'DF590 auth schema remained after downgrade';
        END IF;

        SELECT relrowsecurity, relforcerowsecurity
        INTO state_record
        FROM pg_catalog.pg_class
        WHERE oid = 'public.org_branch_state'::regclass;
        IF NOT state_record.relrowsecurity OR NOT state_record.relforcerowsecurity THEN
            RAISE EXCEPTION
                'DF590 downgrade weakened predecessor org_branch_state RLS';
        END IF;
    END
    $$;
    """)
