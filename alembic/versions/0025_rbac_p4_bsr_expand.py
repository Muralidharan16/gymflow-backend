"""RBAC Hardening Phase 4 — branch_staff_roles Expand Step

Phase 4 (Expand) of the v18.0 hardening plan.

This is the EXPAND step of the Expand/Contract migration pattern.
Old columns (user_id, role ENUM) are kept intact — existing rows and
application code continue to work. New columns are added alongside.

Adds to existing public.branch_staff_roles:
  • organization_member_id UUID NULL  — new tenancy-scoped actor reference
  • role_id SMALLINT NULL             — replaces role ENUM (refs staff_roles)
  • scope_type_id SMALLINT NOT NULL DEFAULT 2  — refs scope_types (default=branch)
  • assignment_source VARCHAR(32) NOT NULL DEFAULT 'dashboard'

New constraints (NOT VALID — added without full table scan lock):
  • fk_bsr_member_id     → organization_members(id)
  • fk_bsr_member_org    → organization_members(id, org_id) composite integrity
  • fk_bsr_role_id       → staff_roles(id)
  • fk_bsr_scope_type_id → scope_types(id)
  • chk_bsr_assignment_src
  • chk_bsr_revocation_from
  • chk_bsr_revocation_to

New exclusion constraint (scoped to rows with organization_member_id set):
  • ex_branch_role_overlap_v2  DEFERRABLE INITIALLY IMMEDIATE

New triggers:
  • app_private.validate_effective_from_window()  — scheduling drift guard
  • app_private.validate_rls_context_match()      — org_id payload poisoning guard

New indexes:
  • ix_bsr_member_active  — primary active lookup for new model
  • ix_bsr_owner_per_org  — supports single-owner enforcement

Hardened RLS:
  • Replaces old tenant_isolation_staff_roles policy
  • fail-closed GUC (false = raises error if unset)
  • soft-delete filter in both USING and WITH CHECK
  • app.can_read_staff_roles GUC pre-authorization

Security barrier view:
  • app_secure.v_active_branch_staff_roles

NOTE: Old columns (user_id, role) remain. Contract step is Phase 8.
NOTE: FK constraints added NOT VALID — run VALIDATE separately post-deploy.

Revision ID: 0025_rbac_p4_bsr_expand
Revises: 0024_rbac_p3_org_members
Create Date: 2026-05-23
"""

from alembic import op

revision = "0025_rbac_p4_bsr_expand"
down_revision = "0024_rbac_p3_org_members"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. Add new columns ────────────────────────────────────────────────

    # organization_member_id: the new tenancy-scoped actor.
    # NULL during dual-write phase; set NOT NULL in Phase 8 (Contract).
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS organization_member_id UUID NULL;
    """)

    op.execute("""
        COMMENT ON COLUMN public.branch_staff_roles.organization_member_id IS
            'Tenancy-scoped actor reference (v18 model). '
            'NULL during expand/dual-write phase. '
            'Set NOT NULL in Phase 8 (contract step) after full backfill.';
    """)

    # role_id: integer FK replacing the ENUM role column.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS role_id SMALLINT NULL;
    """)

    op.execute("""
        COMMENT ON COLUMN public.branch_staff_roles.role_id IS
            'Integer role reference (replaces role ENUM). '
            'NULL during dual-write phase. Set NOT NULL in Phase 8.';
    """)

    # scope_type_id: defaults to branch scope (id=2).
    # NOT NULL with default — safe to add immediately.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS scope_type_id SMALLINT NOT NULL DEFAULT 2;
    """)

    # assignment_source: audit trail for how the assignment was made.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD COLUMN IF NOT EXISTS assignment_source VARCHAR(32) NOT NULL DEFAULT 'dashboard';
    """)

    # ── 2. FK constraints — NOT VALID (no full table scan lock) ──────────
    # Existing rows are NOT validated. Application must backfill before
    # running VALIDATE CONSTRAINT in the post-deploy step.

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_member_id
            FOREIGN KEY (organization_member_id)
            REFERENCES public.organization_members(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    # Composite FK: guarantees (organization_member_id, org_id) cannot
    # reference a member from a different org — prevents cross-tenant corruption.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_member_org
            FOREIGN KEY (organization_member_id, org_id)
            REFERENCES public.organization_members(id, org_id)
            NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_role_id
            FOREIGN KEY (role_id)
            REFERENCES public.staff_roles(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_scope_type_id
            FOREIGN KEY (scope_type_id)
            REFERENCES public.scope_types(id)
            ON DELETE RESTRICT
            NOT VALID;
    """)

    # ── 3. CHECK constraints ──────────────────────────────────────────────

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_assignment_src
            CHECK (assignment_source IN (
                'dashboard', 'api', 'migration', 'bulk_import', 'automation', 'sync_worker'
            ));
    """)

    # Revocation must not predate the start of the assignment
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_revocation_from
            CHECK (revoked_at IS NULL OR revoked_at >= effective_from);
    """)

    # Revocation must not postdate the scheduled end of the assignment
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT chk_bsr_revocation_to
            CHECK (
                effective_to IS NULL
                OR revoked_at IS NULL
                OR revoked_at <= effective_to
            );
    """)

    # ── 4. Temporal exclusion constraint (new model rows only) ────────────
    # Scoped to rows where organization_member_id IS NOT NULL.
    # DEFERRABLE INITIALLY IMMEDIATE allows transactional owner swaps
    # (SET CONSTRAINTS ex_branch_role_overlap_v2 DEFERRED within a txn).
    # btree_gist already installed in Phase 1.
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT ex_branch_role_overlap_v2
        EXCLUDE USING gist (
            organization_member_id  WITH =,
            branch_id               WITH =,
            role_id                 WITH =,
            tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz)) WITH &&
        )
        WHERE (
            organization_member_id IS NOT NULL
            AND role_id IS NOT NULL
            AND revoked_at IS NULL
            AND deleted_at IS NULL
        )
        DEFERRABLE INITIALLY IMMEDIATE;
    """)

    # ── 5. Single active owner constraint ─────────────────────────────────
    # Only one active owner (role_id=1) per org at any time.
    # Partial unique index — only covers rows using the new model.
    op.execute("""
        CREATE UNIQUE INDEX uq_bsr_single_owner_per_org
        ON public.branch_staff_roles(org_id)
        WHERE (
            role_id = 1
            AND revoked_at IS NULL
            AND deleted_at IS NULL
            AND organization_member_id IS NOT NULL
        );
    """)

    # ── 6. Active lookup index for new model ──────────────────────────────
    op.execute("""
        CREATE INDEX ix_bsr_member_active
        ON public.branch_staff_roles(org_id, branch_id, organization_member_id)
        WHERE (
            organization_member_id IS NOT NULL
            AND revoked_at IS NULL
            AND deleted_at IS NULL
        );
    """)

    # ── 7. Scheduling window guard trigger ────────────────────────────────
    # Prevents dormant privilege grants by limiting effective_from to
    # at most 30 days in the future.
    # Uses trigger (not CHECK) because CHECK constraints cannot use
    # volatile functions like clock_timestamp().
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.validate_effective_from_window()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.effective_from > clock_timestamp() + interval '30 days' THEN
                RAISE EXCEPTION
                    'effective_from (%) exceeds the 30-day scheduling window. '
                    'Future-dated assignments beyond 30 days are not permitted.',
                    NEW.effective_from
                USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.validate_effective_from_window() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.validate_effective_from_window() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_bsr_validate_effective_from
            BEFORE INSERT OR UPDATE OF effective_from ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.validate_effective_from_window();
    """)

    # ── 8. RLS context validation trigger ────────────────────────────────
    # Guards against payload org_id poisoning — the org_id in the row
    # being inserted/updated must match the active tenant GUC.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.validate_rls_context_match()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_current_org_id UUID;
        BEGIN
            BEGIN
                v_current_org_id := current_setting('app.current_org_id', false)::uuid;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION
                    'Security context error: app.current_org_id GUC is not set. '
                    'All tenant operations require an active org context.'
                USING ERRCODE = 'insufficient_privilege';
            END;

            IF NEW.org_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION
                    'Security policy violation: row org_id (%) does not match '
                    'active tenant context (%). Cross-tenant write attempt blocked.',
                    NEW.org_id, v_current_org_id
                USING ERRCODE = 'insufficient_privilege';
            END IF;

            RETURN NEW;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.validate_rls_context_match() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.validate_rls_context_match() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_bsr_validate_rls_context
            BEFORE INSERT OR UPDATE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.validate_rls_context_match();
    """)

    # ── 9. Harden RLS policy ─────────────────────────────────────────────
    # Drop the old permissive policy and replace with hardened version.
    # Key changes:
    #   • current_setting(..., false) — raises error if GUC not set (fail-closed)
    #   • deleted_at IS NULL enforced in both USING and WITH CHECK
    #   • app.can_read_staff_roles GUC pre-authorization (set per-transaction by app)
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")

    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles
        ON public.branch_staff_roles
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

    # Ensure FORCE RLS is still active (survives policy replacement)
    op.execute("ALTER TABLE public.branch_staff_roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles FORCE ROW LEVEL SECURITY;")

    # ── 10. Security barrier view ─────────────────────────────────────────
    # Lives in app_secure schema (not public) to isolate from broad grants.
    # Filters deleted + revoked rows centrally — application queries this view.
    op.execute("""
        CREATE OR REPLACE VIEW app_secure.v_active_branch_staff_roles
        WITH (security_barrier = true)
        AS
        SELECT
            bsr.id,
            bsr.org_id,
            bsr.branch_id,
            bsr.organization_member_id,
            bsr.role_id,
            sr.code          AS role_code,
            sr.hierarchy_level,
            bsr.scope_type_id,
            st.code          AS scope_code,
            bsr.assignment_source,
            bsr.assigned_by,
            bsr.assigned_at,
            bsr.effective_from,
            bsr.effective_to,
            -- Legacy columns (present during dual-write phase)
            bsr.user_id,
            bsr.role         AS role_legacy,
            bsr.created_at
        FROM public.branch_staff_roles bsr
        LEFT JOIN public.staff_roles  sr ON sr.id = bsr.role_id
        LEFT JOIN public.scope_types  st ON st.id = bsr.scope_type_id
        WHERE bsr.deleted_at IS NULL
          AND bsr.revoked_at IS NULL;
    """)

    op.execute("""
        GRANT SELECT ON app_secure.v_active_branch_staff_roles
        TO app_runtime, readonly_analytics;
    """)

    op.execute("""
        COMMENT ON VIEW app_secure.v_active_branch_staff_roles IS
            'Security-barrier view of active branch staff role assignments. '
            'Joins staff_roles and scope_types for human-readable codes. '
            'Application code should query this view, not the base table directly.';
    """)

    # ── 11. Grants on new columns / table ─────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.branch_staff_roles
        TO app_runtime;
    """)
    op.execute("GRANT SELECT ON public.branch_staff_roles TO audit_writer, readonly_analytics;")


def downgrade() -> None:
    # View
    op.execute("DROP VIEW IF EXISTS app_secure.v_active_branch_staff_roles;")

    # Triggers
    op.execute("DROP TRIGGER IF EXISTS trg_bsr_validate_rls_context ON public.branch_staff_roles;")
    op.execute("DROP TRIGGER IF EXISTS trg_bsr_validate_effective_from ON public.branch_staff_roles;")

    # Trigger functions
    op.execute("DROP FUNCTION IF EXISTS app_private.validate_rls_context_match();")
    op.execute("DROP FUNCTION IF EXISTS app_private.validate_effective_from_window();")

    # Restore original (weaker) RLS policy
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles ON public.branch_staff_roles
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    # Indexes
    op.execute("DROP INDEX IF EXISTS ix_bsr_member_active;")
    op.execute("DROP INDEX IF EXISTS uq_bsr_single_owner_per_org;")

    # Constraints
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS ex_branch_role_overlap_v2;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_revocation_to;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_revocation_from;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS chk_bsr_assignment_src;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_scope_type_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_role_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_member_org;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_member_id;")

    # Columns
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS assignment_source;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS scope_type_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS role_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS organization_member_id;")
