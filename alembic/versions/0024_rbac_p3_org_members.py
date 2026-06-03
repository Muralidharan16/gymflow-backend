"""RBAC Hardening Phase 3 — organization_members Table

Phase 3 of the v18.0 hardening plan.

Creates:
  • public.organization_members
      — The core tenancy boundary separating global identity from
        scoped authorization.
      — References organization_users(id, org_id) via composite FK.
      — Holds membership lifecycle state (references membership_statuses).
      — Exposes UNIQUE (id, org_id) for downstream composite FKs
        (branch_staff_roles Phase 5).

  • app_private.touch_updated_at()
      — Generic BEFORE UPDATE trigger to auto-maintain updated_at.

  • app_private.enforce_membership_state_transition()
      — State machine guard: prevents invalid lifecycle transitions.
        Terminal state: revoked (id=5) cannot transition back to pending (id=1).

  • RLS policy: tenant_isolation_organization_members
      — Fail-closed: requires app.current_org_id GUC to be explicitly set.
      — Excludes soft-deleted rows from all reads and writes.

Does NOT modify branch_staff_roles. That is Phase 5 (Expand step).

Revision ID: 0024_rbac_p3_org_members
Revises: 0023_rbac_p2_ref_tables
Create Date: 2026-05-23
"""

from alembic import op

revision = "0024_rbac_p3_org_members"
down_revision = "0023_rbac_p2_ref_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. organization_members table ─────────────────────────────────────
    # References organization_users (existing user identity table) and
    # organizations. membership_status_id references the new ref table.
    op.execute("""
        CREATE TABLE public.organization_members (
            id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id               UUID        NOT NULL
                                 REFERENCES public.organizations(id) ON DELETE RESTRICT,
            user_id              UUID        NOT NULL
                                 REFERENCES public.organization_users(id) ON DELETE RESTRICT,
            membership_status_id SMALLINT    NOT NULL DEFAULT 1
                                 REFERENCES public.membership_statuses(id) ON DELETE RESTRICT,
            permission_version   BIGINT      NOT NULL DEFAULT 1,
            region_id            UUID        NULL,

            created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at           TIMESTAMPTZ NULL,
            deleted_by           UUID        NULL
                                 REFERENCES public.organization_users(id) ON DELETE RESTRICT,

            -- Natural uniqueness: one membership record per user per org
            CONSTRAINT uq_org_member_user  UNIQUE (org_id, user_id),

            -- Composite candidate key: required for composite FKs from branch_staff_roles
            -- (organization_member_id, org_id) -> (id, org_id)
            CONSTRAINT uq_org_member_pair  UNIQUE (id, org_id)
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.organization_members IS
            'Core tenancy boundary: separates global identity (organization_users) '
            'from scoped authorization (branch_staff_roles). '
            'One row per user per organization. '
            'uq_org_member_pair supports composite FK from branch_staff_roles — do not drop.';
    """)

    op.execute("""
        COMMENT ON CONSTRAINT uq_org_member_pair ON public.organization_members IS
            'Required for composite FK (organization_member_id, org_id) from branch_staff_roles. Do not drop.';
    """)

    # ── 2. Indexes ────────────────────────────────────────────────────────

    # Primary access path: find active members in an org by user
    op.execute("""
        CREATE INDEX ix_org_members_active
        ON public.organization_members(org_id, user_id)
        WHERE deleted_at IS NULL;
    """)

    # Status filter (for suspension/revocation batch operations)
    op.execute("""
        CREATE INDEX ix_org_members_status
        ON public.organization_members(org_id, membership_status_id)
        WHERE deleted_at IS NULL;
    """)

    # ── 3. touch_updated_at trigger function ──────────────────────────────
    # Generic reusable trigger: updates updated_at on any row modification.
    # Owned by app_security_owner; revoked from PUBLIC.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.touch_updated_at()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.touch_updated_at() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.touch_updated_at() FROM PUBLIC;")
    op.execute("""
        COMMENT ON FUNCTION app_private.touch_updated_at() IS
            'Generic BEFORE UPDATE trigger to auto-maintain updated_at column. '
            'Attach with: CREATE TRIGGER ... BEFORE UPDATE ON <table> FOR EACH ROW EXECUTE FUNCTION app_private.touch_updated_at()';
    """)

    op.execute("""
        CREATE TRIGGER trg_touch_organization_members_updated_at
            BEFORE UPDATE ON public.organization_members
            FOR EACH ROW
            EXECUTE FUNCTION app_private.touch_updated_at();
    """)

    # ── 4. State machine transition guard ─────────────────────────────────
    # Enforces valid membership lifecycle transitions at the DB layer.
    # Terminal rule: revoked (id=5) cannot go back to pending (id=1).
    # Extend this function as more transition rules are needed.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.enforce_membership_state_transition()
        RETURNS TRIGGER
        STRICT
        VOLATILE
        PARALLEL UNSAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Rule 1: revoked is terminal — cannot transition back to pending
            IF OLD.membership_status_id = 5 AND NEW.membership_status_id = 1 THEN
                RAISE EXCEPTION
                    'Invalid membership state transition: revoked (%) -> pending (%) is not permitted. '
                    'Revoked membership is terminal.',
                    OLD.membership_status_id, NEW.membership_status_id
                USING ERRCODE = 'check_violation';
            END IF;

            -- Rule 2: expired is terminal — cannot be reactivated directly
            IF OLD.membership_status_id = 6 AND NEW.membership_status_id = 3 THEN
                RAISE EXCEPTION
                    'Invalid membership state transition: expired (%) -> active (%) is not permitted. '
                    'Create a new membership record instead.',
                    OLD.membership_status_id, NEW.membership_status_id
                USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.enforce_membership_state_transition() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.enforce_membership_state_transition() FROM PUBLIC;")
    op.execute("""
        COMMENT ON FUNCTION app_private.enforce_membership_state_transition() IS
            'State machine guard for organization_members.membership_status_id. '
            'Terminal states: revoked (5) and expired (6) cannot be directly reactivated. '
            'Extend this function to add more transition rules.';
    """)

    op.execute("""
        CREATE TRIGGER trg_membership_state_transition
            BEFORE UPDATE OF membership_status_id ON public.organization_members
            FOR EACH ROW
            WHEN (OLD.membership_status_id IS DISTINCT FROM NEW.membership_status_id)
            EXECUTE FUNCTION app_private.enforce_membership_state_transition();
    """)

    # ── 5. RLS policies ───────────────────────────────────────────────────
    op.execute("ALTER TABLE public.organization_members ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_members FORCE ROW LEVEL SECURITY;")

    # Fail-closed: current_setting(..., false) raises an error if GUC is not set.
    # deleted_at IS NULL enforces soft-delete in both read and write paths.
    op.execute("""
        CREATE POLICY tenant_isolation_organization_members
        ON public.organization_members
        FOR ALL
        USING (
            org_id    = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
        )
        WITH CHECK (
            org_id    = current_setting('app.current_org_id', false)::uuid
            AND deleted_at IS NULL
        );
    """)

    # ── 6. Grants ─────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.organization_members
        TO app_runtime;
    """)
    # audit_writer needs read access to snapshot actor membership details
    op.execute("GRANT SELECT ON public.organization_members TO audit_writer;")
    op.execute("GRANT SELECT ON public.organization_members TO readonly_analytics;")

    # Grant sequence for BIGINT permission_version (not a serial, but good practice)
    # No sequence needed — permission_version is a plain BIGINT updated by app.


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_membership_state_transition ON public.organization_members;")
    op.execute("DROP TRIGGER IF EXISTS trg_touch_organization_members_updated_at ON public.organization_members;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_organization_members ON public.organization_members;")

    op.execute("DROP INDEX IF EXISTS ix_org_members_status;")
    op.execute("DROP INDEX IF EXISTS ix_org_members_active;")

    op.execute("DROP TABLE IF EXISTS public.organization_members CASCADE;")

    op.execute("DROP FUNCTION IF EXISTS app_private.enforce_membership_state_transition();")
    op.execute("DROP FUNCTION IF EXISTS app_private.touch_updated_at();")
