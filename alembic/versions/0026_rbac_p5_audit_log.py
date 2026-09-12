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
import sqlalchemy as sa

revision = "0026_rbac_p5_audit_log"
down_revision = "0025_rbac_p4_bsr_expand"
branch_labels = None
depends_on = None

# RB1M2N_0026_APP_PRIVATE_OWNER_TRANSFER_SCHEMA_ACL_HELPERS

_RB1M2N_PRIVATE_SCHEMA = "app_private"
_RB1M2N_TARGET_OWNER = "app_security_owner"
_RB1M2N_FUNCTION_OWNER_CONTRACT = (
    ("append_audit_event", 14),
    ("ensure_future_partition", 2),
    ("org_advisory_lock_key", 1),
    ("raise_immutable_audit_violation", 0),
)


def _rb1m2n_identity(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()


def _rb1m2n_require_migration_owner(bind):
    identity = _rb1m2n_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError(
            "Revision 0026 requires session_user migration_owner; observed "
            f"{identity['session_user_name']!r}."
        )
    if identity["current_user_name"] != "migration_owner":
        raise RuntimeError(
            "Revision 0026 requires current_user migration_owner; observed "
            f"{identity['current_user_name']!r}."
        )


def _rb1m2n_direct_private_create_acl(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                grantee_role.rolname::text AS grantee_name,
                schema_acl.privilege_type::text AS privilege_type,
                schema_acl.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    namespace_data.nspacl,
                    pg_catalog.acldefault('n', namespace_data.nspowner)
                )
            ) AS schema_acl
            JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = schema_acl.grantor
            JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = schema_acl.grantee
            WHERE namespace_data.nspname = 'app_private'
              AND grantee_role.rolname = 'app_security_owner'
              AND schema_acl.privilege_type = 'CREATE'
            ORDER BY
                grantor_name,
                grantee_name,
                privilege_type,
                is_grantable
            """
        )
    ).all()
    return tuple((row[0], row[1], row[2], bool(row[3])) for row in rows)


def _rb1m2n_preflight_private_owner_transfer(bind):
    _rb1m2n_require_migration_owner(bind)
    schema_row = bind.execute(
        sa.text(
            """
            SELECT owner_role.rolname::text AS owner_name
            FROM pg_catalog.pg_namespace AS namespace_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = namespace_data.nspowner
            WHERE namespace_data.nspname = 'app_private'
            """
        )
    ).mappings().one_or_none()
    if schema_row is None:
        raise RuntimeError("Required schema app_private is absent.")
    if schema_row["owner_name"] != "migration_owner":
        raise RuntimeError(
            "app_private must remain owned by migration_owner; observed "
            f"{schema_row['owner_name']!r}."
        )
    target_exists = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles
                WHERE rolname = 'app_security_owner'
            )
            """
        )
    ).scalar_one()
    if target_exists is not True:
        raise RuntimeError("Required role app_security_owner is absent.")
    can_set_role = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_has_role(
                session_user,
                'app_security_owner',
                'SET'
            )
            """
        )
    ).scalar_one()
    if can_set_role is not True:
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner.")
    has_usage = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                'app_security_owner',
                'app_private',
                'USAGE'
            )
            """
        )
    ).scalar_one()
    if has_usage is not True:
        raise RuntimeError(
            "app_security_owner lacks required USAGE on app_private."
        )


def _rb1m2n_prepare_private_create_acl(bind):
    _rb1m2n_preflight_private_owner_transfer(bind)
    before = _rb1m2n_direct_private_create_acl(bind)
    has_create = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                'app_security_owner',
                'app_private',
                'CREATE'
            )
            """
        )
    ).scalar_one()
    added = has_create is not True
    if added:
        bind.execute(
            sa.text(
                "GRANT CREATE ON SCHEMA app_private "
                "TO app_security_owner"
            )
        )
    effective_after = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                'app_security_owner',
                'app_private',
                'CREATE'
            )
            """
        )
    ).scalar_one()
    if effective_after is not True:
        raise RuntimeError(
            "app_security_owner did not acquire effective CREATE on app_private."
        )
    return before, added


def _rb1m2n_restore_private_create_acl(bind, before, added):
    _rb1m2n_require_migration_owner(bind)
    if added:
        bind.execute(
            sa.text(
                "REVOKE CREATE ON SCHEMA app_private "
                "FROM app_security_owner"
            )
        )
    observed = _rb1m2n_direct_private_create_acl(bind)
    if observed != before:
        raise RuntimeError(
            "app_private CREATE ACL restoration drift: "
            f"observed={observed!r}, expected={before!r}."
        )


def _rb1m2n_verify_function_owner_contract(bind):
    _rb1m2n_require_migration_owner(bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.proname::text AS function_name,
                procedure_data.pronargs::int AS argument_count,
                owner_role.rolname::text AS owner_name,
                COUNT(*) FILTER (
                    WHERE function_acl.grantee = 0
                      AND function_acl.privilege_type = 'EXECUTE'
                )::int AS public_execute_count
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = procedure_data.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure_data.proacl,
                    pg_catalog.acldefault('f', procedure_data.proowner)
                )
            ) AS function_acl
            WHERE namespace_data.nspname = 'app_private'
              AND (
                    (procedure_data.proname = 'append_audit_event'
                     AND procedure_data.pronargs = 14)
                 OR (procedure_data.proname = 'ensure_future_partition'
                     AND procedure_data.pronargs = 2)
                 OR (procedure_data.proname = 'org_advisory_lock_key'
                     AND procedure_data.pronargs = 1)
                 OR (procedure_data.proname = 'raise_immutable_audit_violation'
                     AND procedure_data.pronargs = 0)
              )
            GROUP BY
                procedure_data.proname,
                procedure_data.pronargs,
                owner_role.rolname
            ORDER BY function_name, argument_count
            """
        )
    ).all()
    observed = tuple(
        (row[0], int(row[1]), row[2], int(row[3]))
        for row in rows
    )
    expected = tuple(
        (name, count, _RB1M2N_TARGET_OWNER, 0)
        for name, count in _RB1M2N_FUNCTION_OWNER_CONTRACT
    )
    if observed != expected:
        raise RuntimeError(
            "Revision-0026 function owner contract drift: "
            f"observed={observed!r}, expected={expected!r}."
        )



def upgrade() -> None:
    bind = op.get_bind()
    private_create_acl_before, private_create_added = (
        _rb1m2n_prepare_private_create_acl(bind)
    )

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

    op.execute("REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.org_advisory_lock_key(UUID) TO audit_writer;")
    op.execute("ALTER FUNCTION app_private.org_advisory_lock_key(UUID) OWNER TO app_security_owner;")

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

    op.execute("REVOKE ALL ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) TO audit_writer;")
    op.execute("ALTER FUNCTION app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID) OWNER TO app_security_owner;")

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

    op.execute("REVOKE ALL ON FUNCTION app_private.ensure_future_partition(TEXT, INT) FROM PUBLIC;")
    op.execute("ALTER FUNCTION app_private.ensure_future_partition(TEXT, INT) OWNER TO app_security_owner;")

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

    # Keep migration_owner as the trigger-function owner until all seed
    # partitions exist. PostgreSQL clones the parent trigger onto each new
    # partition, and the creating role must retain authority on the trigger
    # function throughout that operation.
    op.execute("ALTER FUNCTION app_private.raise_immutable_audit_violation() OWNER TO app_security_owner;")
    _rb1m2n_restore_private_create_acl(
        bind, private_create_acl_before, private_create_added
    )
    _rb1m2n_verify_function_owner_contract(bind)

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

    # Revision 0025 already enabled RLS but did not force it. Restore only
    # the force-state changed by revision 0026; never disable tenant RLS.
    op.execute(
        "ALTER TABLE public.branch_audit_log "
        "NO FORCE ROW LEVEL SECURITY;"
    )

    # Reverse exactly the table privileges introduced by this revision.
    op.execute(
        "REVOKE INSERT, SELECT ON public.branch_audit_log "
        "FROM audit_writer;"
    )
    op.execute(
        "REVOKE SELECT ON public.branch_audit_log "
        "FROM app_runtime, readonly_analytics;"
    )

    # Triggers
    op.execute("DROP TRIGGER IF EXISTS trg_deny_audit_mutation ON public.branch_audit_log;")

    # Seeded partitions introduced by this revision. RESTRICT is
    # intentional: downgrade must fail closed if a later object depends on
    # one of these partitions.
    op.execute("DROP TABLE IF EXISTS public.branch_audit_log_y2026_m06 RESTRICT;")
    op.execute("DROP TABLE IF EXISTS public.branch_audit_log_y2026_m07 RESTRICT;")
    op.execute("DROP TABLE IF EXISTS public.branch_audit_log_y2026_m08 RESTRICT;")

    # The functions are owned by app_security_owner at revision 0026. SET
    # LOCAL ROLE is transaction-scoped. RESET ROLE runs only on the success
    # path; if a protected drop fails, rollback restores the role and the
    # original PostgreSQL exception remains unmasked.
    op.execute("SET LOCAL ROLE app_security_owner;")
    op.execute("DROP FUNCTION IF EXISTS app_private.ensure_future_partition(TEXT, INT);")
    op.execute("DROP FUNCTION IF EXISTS app_private.append_audit_event(UUID,UUID,UUID,VARCHAR,VARCHAR,TEXT,JSONB,UUID,JSONB,JSONB,TEXT,VARCHAR,VARCHAR,UUID);")
    op.execute("DROP FUNCTION IF EXISTS app_private.org_advisory_lock_key(UUID);")
    op.execute("DROP FUNCTION IF EXISTS app_private.raise_immutable_audit_violation();")
    op.execute("RESET ROLE;")

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
