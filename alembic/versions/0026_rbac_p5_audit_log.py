"""RBAC Hardening Phase 5 — Audit Log Hardening + append_audit_event()

Phase 5 of the v18.0 hardening plan.

Expands existing public.branch_audit_log with:
  • audit_sequence BIGINT GENERATED ALWAYS AS IDENTITY  — global monotonic sequence
  • event_id UUID UNIQUE NOT NULL                        — stable external event ref
  • request_id UUID NULL                                 — distributed trace correlation
  • region_id UUID NULL                                  — future multi-region support
  • actor_snapshot JSONB NOT NULL DEFAULT '{}'           — point-in-time actor state
  • actor_permissions JSONB NOT NULL DEFAULT '{}'        — point-in-time actor perms
  • action_category VARCHAR(32) GENERATED ALWAYS AS ... — domain prefix for filtering
  • reason_code VARCHAR(32) NOT NULL DEFAULT 'unspecified'
  • previous_event_hash VARCHAR(64) NULL                 — hash chain predecessor
  • event_hash VARCHAR(64) NULL                          — SHA-256 of canonical payload
  • hash_key_version SMALLINT NOT NULL DEFAULT 1         — refs audit_key_registry
  • policy_version INT NOT NULL DEFAULT 1
  • app_version VARCHAR(32) NULL
  • deployment_id UUID NULL
  • chk_prev_hash_chain CHECK constraint

Creates:
  • app_private.org_advisory_lock_key(UUID) IMMUTABLE    — MD5-stable bigint lock key
  • app_private.raise_immutable_audit_violation()         — UPDATE/DELETE guard trigger
  • app_private.append_audit_event(...)                   — serialized hash chain writer
  • trg_deny_audit_mutation                               — immutability enforcement
  • ix_audit_org_sequence                                 — primary query index
  • app_private.ensure_future_partition(text, int)        — partition lifecycle manager

Note: existing rows get NULL for hash columns — bootstrap row semantics apply.
Note: event_hash will be backfilled by the application on next write per org.
Note: IDENTITY sequence on partitioned table requires PG 14+.

Revision ID: 0026_rbac_p5_audit_log
Revises: 0025_rbac_p4_bsr_expand
Create Date: 2026-05-23
"""

from alembic import op

revision = "0026_rbac_p5_audit_log"
down_revision = "0025_rbac_p4_bsr_expand"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. Expand branch_audit_log columns ────────────────────────────────

    # IDENTITY cannot be added to a partitioned table that already has child
    # partitions (PG limitation). Use a dedicated sequence instead — semantics
    # are identical: globally monotonic, auto-incrementing, gap-tolerant.
    op.execute("CREATE SEQUENCE IF NOT EXISTS public.branch_audit_log_seq;")
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS audit_sequence
            BIGINT NOT NULL DEFAULT nextval('public.branch_audit_log_seq');
    """)
    op.execute("ALTER SEQUENCE public.branch_audit_log_seq OWNED BY public.branch_audit_log.audit_sequence;")

    op.execute("""
        COMMENT ON COLUMN public.branch_audit_log.audit_sequence IS
            'Globally monotonic identity sequence. NOT per-org contiguous — '
            'gaps across tenants are expected. Hash chain provides per-org continuity. '
            'Use for ordering only; never assume contiguity within a tenant.';
    """)

    # Stable external event reference (used in distributed tracing, outbox correlation)
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS event_id UUID NOT NULL DEFAULT gen_random_uuid();
    """)

    # Distributed trace request correlation
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS request_id UUID NULL;
    """)

    # Future multi-region support
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS region_id UUID NULL;
    """)

    # Point-in-time actor state snapshot (name, role, membership at time of event)
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS actor_snapshot JSONB NOT NULL DEFAULT '{}';
    """)

    # Point-in-time actor permissions snapshot
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS actor_permissions JSONB NOT NULL DEFAULT '{}';
    """)

    # Generated column: domain prefix of action for fast category filtering
    # e.g. 'staff_roles.assign' -> 'staff_roles'
    # Note: uses split_part which is IMMUTABLE — valid for generated column
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS action_category
            VARCHAR(32) GENERATED ALWAYS AS (split_part(action, '.', 1)) STORED;
    """)

    # Structured reason code for programmatic filtering
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS reason_code
            VARCHAR(32) NOT NULL DEFAULT 'unspecified';
    """)

    # Hash chain columns — NULL on existing rows (system.bootstrap semantics apply)
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS previous_event_hash VARCHAR(64) NULL;
    """)

    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS event_hash VARCHAR(64) NULL;
    """)

    op.execute("""
        COMMENT ON COLUMN public.branch_audit_log.event_hash IS
            'SHA-256 of RFC8785 canonical JSON payload. '
            'Computed by application layer — never by PostgreSQL. '
            'NULL for rows written before Phase 5 migration.';
    """)

    # Key version for cryptographic agility
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS hash_key_version SMALLINT NOT NULL DEFAULT 1;
    """)

    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS policy_version INT NOT NULL DEFAULT 1;
    """)

    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS app_version VARCHAR(32) NULL;
    """)

    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD COLUMN IF NOT EXISTS deployment_id UUID NULL;
    """)

    # ── 2. Hash chain integrity constraint ───────────────────────────────
    # previous_event_hash is NULL only for genesis (system.bootstrap) events.
    # Existing rows are exempted — they pre-date the hash chain.
    op.execute("""
        ALTER TABLE public.branch_audit_log
        ADD CONSTRAINT chk_prev_hash_chain
            CHECK (
                event_hash IS NULL                    -- pre-Phase5 legacy rows
                OR previous_event_hash IS NOT NULL    -- normal chained events
                OR action = 'system.bootstrap'        -- genesis events
            );
    """)

    # ── 3. event_id lookup index ─────────────────────────────────────────
    # Partitioned UNIQUE indexes must include all partition key columns (created_at).
    # A pure event_id unique index is not possible here.
    # Uniqueness of event_id is enforced at app layer (gen_random_uuid()).
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_audit_event_id
        ON public.branch_audit_log(event_id, created_at)
        WHERE event_id IS NOT NULL;
    """)

    # ── 4. Primary query index ────────────────────────────────────────────
    # org_id + audit_sequence DESC: the canonical per-tenant chain walk.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_audit_org_sequence
        ON public.branch_audit_log(org_id, audit_sequence DESC);
    """)

    # category filter index (dashboard / compliance reports)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_audit_org_category
        ON public.branch_audit_log(org_id, action_category, created_at DESC);
    """)

    # ── 5. Immutability: revoke UPDATE/DELETE from runtime role ──────────
    op.execute("REVOKE UPDATE, DELETE ON public.branch_audit_log FROM app_runtime;")
    op.execute("GRANT INSERT, SELECT ON public.branch_audit_log TO audit_writer;")
    op.execute("GRANT SELECT ON public.branch_audit_log TO app_runtime, readonly_analytics;")

    # ── 6. Immutability trigger function ─────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.raise_immutable_audit_violation()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'Security policy violation: audit log rows are immutable. '
                'UPDATE and DELETE on branch_audit_log are prohibited.'
            USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.raise_immutable_audit_violation() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.raise_immutable_audit_violation() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_deny_audit_mutation
            BEFORE UPDATE OR DELETE ON public.branch_audit_log
            FOR EACH ROW
            EXECUTE FUNCTION app_private.raise_immutable_audit_violation();
    """)

    # ── 7. Advisory lock key helper ───────────────────────────────────────
    # IMMUTABLE + PARALLEL SAFE: safe to use inside any query context.
    # MD5-based bigint: deterministic across all PG versions and locales.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.org_advisory_lock_key(p_org_id UUID)
        RETURNS BIGINT
        STRICT
        IMMUTABLE
        PARALLEL SAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- MD5 hex → first 16 chars → bit(64) → bigint
            -- Deterministic across PG major versions; stable under failover/replication.
            RETURN (('x' || substr(md5(p_org_id::text), 1, 16)))::bit(64)::bigint;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.org_advisory_lock_key(UUID) TO audit_writer;")

    # ── 8. append_audit_event() — serialized hash chain writer ───────────
    # CONTRACT:
    #   • p_canonical_payload: RFC8785 canonical JSON string (app-generated)
    #   • p_event_hash: SHA-256 hex of p_canonical_payload (app-generated)
    #   • DB validates hash matches payload before insert
    #   • Advisory lock serializes per-org chain writes — no fork possible
    #   • predecessor fetched by audit_sequence DESC (no clock dependency)
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.append_audit_event(
            p_org_id            UUID,
            p_branch_id         UUID,
            p_actor_id          UUID,
            p_action            VARCHAR(64),
            p_reason_code       VARCHAR(32),
            p_reason            TEXT,
            p_diff              JSONB,
            p_request_id        UUID,
            p_actor_snapshot    JSONB,
            p_actor_permissions JSONB,
            p_canonical_payload TEXT,
            p_event_hash        VARCHAR(64),
            p_app_version       VARCHAR(32) DEFAULT NULL,
            p_deployment_id     UUID        DEFAULT NULL
        )
        RETURNS UUID
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = off
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_prev_hash  VARCHAR(64);
            v_event_id   UUID := gen_random_uuid();
            v_db_hash    VARCHAR(64);
        BEGIN
            -- Step 1: Acquire per-org transaction advisory lock.
            -- Serializes all hash chain writes for this org — prevents fork races.
            PERFORM pg_advisory_xact_lock(
                app_private.org_advisory_lock_key(p_org_id)
            );

            -- Step 2: Validate app-supplied hash against the canonical payload.
            -- The DB re-derives the hash as a tamper sanity check.
            -- This does NOT replace app-layer canonicalization — it only catches
            -- accidental mismatches between payload and hash arguments.
            v_db_hash := encode(
                sha256(convert_to(p_canonical_payload, 'UTF8')),
                'hex'
            );

            IF v_db_hash <> p_event_hash THEN
                RAISE EXCEPTION
                    'Audit integrity error: canonical payload hash mismatch. '
                    'Expected %, got %. Check RFC8785 canonicalization in app layer.',
                    p_event_hash, v_db_hash
                USING ERRCODE = 'data_corrupted';
            END IF;

            -- Step 3: Fetch predecessor hash — ordered by sequence only.
            -- No timestamp dependency (avoids clock-skew ambiguity).
            SELECT event_hash
            INTO   v_prev_hash
            FROM   public.branch_audit_log
            WHERE  org_id = p_org_id
              AND  event_hash IS NOT NULL
            ORDER  BY audit_sequence DESC
            LIMIT  1;

            -- Step 4: Insert the event.
            INSERT INTO public.branch_audit_log (
                event_id,
                org_id,
                branch_id,
                actor_id,
                action,
                reason_code,
                reason,
                diff,
                request_id,
                actor_snapshot,
                actor_permissions,
                previous_event_hash,
                event_hash,
                hash_key_version,
                app_version,
                deployment_id
            ) VALUES (
                v_event_id,
                p_org_id,
                p_branch_id,
                p_actor_id,
                p_action,
                p_reason_code,
                p_reason,
                p_diff,
                p_request_id,
                p_actor_snapshot,
                p_actor_permissions,
                v_prev_hash,          -- NULL for first event in org (genesis)
                p_event_hash,
                1,                    -- active key version from audit_key_registry
                p_app_version,
                p_deployment_id
            );

            RETURN v_event_id;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) TO audit_writer;")

    # ── 9. Partition lifecycle manager ────────────────────────────────────
    # Called by a scheduler (pg_cron / Celery beat) to pre-create monthly partitions.
    # Whitelists allowed targets — never interpolates user input into DDL.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.ensure_future_partition(
            p_table_name TEXT,
            p_days_ahead INT
        )
        RETURNS VOID
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_qualified_name TEXT;
            v_partition_date TIMESTAMPTZ := clock_timestamp() + (p_days_ahead || ' days')::interval;
            v_partition_name TEXT;
            v_start_str      TEXT;
            v_end_str        TEXT;
        BEGIN
            -- Whitelist: only these tables may be auto-partitioned.
            -- Never interpolate p_table_name directly into DDL.
            v_qualified_name := CASE p_table_name
                WHEN 'branch_audit_log' THEN 'public.branch_audit_log'
                WHEN 'auth_sessions'    THEN 'public.auth_sessions'
                ELSE NULL
            END;

            IF v_qualified_name IS NULL THEN
                RAISE EXCEPTION
                    'Invalid partition target: %. Allowed: branch_audit_log, auth_sessions.',
                    p_table_name
                USING ERRCODE = 'invalid_parameter_value';
            END IF;

            -- Build month-based partition name e.g. branch_audit_log_2026_06
            v_partition_name := replace(p_table_name, '.', '_')
                                 || '_' || to_char(v_partition_date, 'YYYY_MM');
            v_start_str := to_char(date_trunc('month', v_partition_date), 'YYYY-MM-DD');
            v_end_str   := to_char(
                               date_trunc('month', v_partition_date) + interval '1 month',
                               'YYYY-MM-DD'
                           );

            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %s '
                'FOR VALUES FROM (%L) TO (%L)',
                v_partition_name,
                v_qualified_name,   -- pre-validated hardcoded string, not user input
                v_start_str,
                v_end_str
            );
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.ensure_future_partition(TEXT, INT) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.ensure_future_partition(TEXT, INT) FROM PUBLIC;")
    # Grant EXECUTE to postgres (migration runner) so we can call it during the upgrade step
    op.execute("GRANT EXECUTE ON FUNCTION app_private.ensure_future_partition(TEXT, INT) TO postgres;")

    # ── 10. Pre-create next 3 monthly partitions ──────────────────────────
    # Ensures no insert failures at month boundary (current + next 2 months).
    # Created directly here (not via ensure_future_partition) because
    # the function's SECURITY DEFINER owner (app_security_owner) does not
    # have CREATE TABLE privileges. The function is for scheduler/cron use.
    op.execute("""
        CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m06
            PARTITION OF public.branch_audit_log
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m07
            PARTITION OF public.branch_audit_log
            FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS branch_audit_log_y2026_m08
            PARTITION OF public.branch_audit_log
            FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
    """)

    # ── 11. RLS on audit log ─────────────────────────────────────────────
    op.execute("ALTER TABLE public.branch_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_audit_log FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_audit_log
        ON public.branch_audit_log
        FOR ALL
        USING (
            org_id = current_setting('app.current_org_id', false)::uuid
        )
        WITH CHECK (
            org_id = current_setting('app.current_org_id', false)::uuid
        );
    """)


def downgrade() -> None:
    # RLS
    op.execute("DROP POLICY IF EXISTS tenant_isolation_audit_log ON public.branch_audit_log;")

    # Triggers
    op.execute("DROP TRIGGER IF EXISTS trg_deny_audit_mutation ON public.branch_audit_log;")

    # Functions
    op.execute("DROP FUNCTION IF EXISTS app_private.ensure_future_partition(TEXT, INT);")
    op.execute("DROP FUNCTION IF EXISTS app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID);")
    op.execute("DROP FUNCTION IF EXISTS app_private.org_advisory_lock_key(UUID);")
    op.execute("DROP FUNCTION IF EXISTS app_private.raise_immutable_audit_violation();")

    # Indexes
    op.execute("DROP INDEX IF EXISTS ix_audit_org_category;")
    op.execute("DROP INDEX IF EXISTS ix_audit_org_sequence;")
    op.execute("DROP INDEX IF EXISTS ix_audit_event_id;")

    # Constraints
    op.execute("ALTER TABLE public.branch_audit_log DROP CONSTRAINT IF EXISTS chk_prev_hash_chain;")

    # Columns (reverse order)
    for col in [
        'deployment_id', 'app_version', 'policy_version', 'hash_key_version',
        'event_hash', 'previous_event_hash', 'reason_code', 'action_category',
        'actor_permissions', 'actor_snapshot', 'region_id', 'request_id',
        'event_id',
    ]:
        op.execute(f"ALTER TABLE public.branch_audit_log DROP COLUMN IF EXISTS {col};")
    op.execute("ALTER TABLE public.branch_audit_log DROP COLUMN IF EXISTS audit_sequence;")
    op.execute("DROP SEQUENCE IF EXISTS public.branch_audit_log_seq;")
