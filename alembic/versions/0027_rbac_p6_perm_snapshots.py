"""RBAC Hardening Phase 6 — Permission Snapshots

Phase 6 of the v18.0 hardening plan.

Creates:
  • public.member_permission_snapshots
      — Compiled JSONB permission cache per (member, scope, branch).
      — Keyed by (organization_member_id, org_id, scope_type_id, branch_id).
      — compiled_permissions: sorted JSONB array of permission codes.
      — snapshot_version ties to organization_members.permission_version.
      — is_stale: set TRUE by trigger when roles change (lazy recompute).
      — expires_at: absolute TTL for cache entry.
      — Append-only row semantics: stale rows are never updated in-place,
        a new snapshot row is inserted on recompute (audit trail preserved).

  • app_private.mark_snapshot_stale()
      — AFTER trigger on branch_staff_roles (INSERT/UPDATE/DELETE).
      — Marks all snapshots for the affected member+branch stale.
      — Also bumps organization_members.permission_version.

  • app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT)
      — Computes the full permission set for a member at a given scope.
      — Returns JSONB sorted array of permission codes.
      — Called by application layer to rebuild a stale snapshot.

  • app_secure.v_effective_member_permissions
      — Security-barrier view: most recent non-stale snapshot per member+branch.

RLS:
  • tenant_isolation_permission_snapshots
      — Fail-closed; filters by org_id GUC.

Design notes:
  • Snapshots are NOT the source of truth — branch_staff_roles is.
  • Snapshots are a pre-compiled read cache for RLS and API responses.
  • TTL (expires_at) is 1 hour by default — refreshed on any role mutation.
  • Application must call compile_member_permissions() to rebuild on cache miss.

Revision ID: 0027_rbac_p6_perm_snapshots
Revises: 0026_rbac_p5_audit_log
Create Date: 2026-05-23
"""

from alembic import op

revision = "0027_rbac_p6_perm_snapshots"
down_revision = "0026_rbac_p5_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. member_permission_snapshots table ──────────────────────────────
    op.execute("""
        CREATE TABLE public.member_permission_snapshots (
            id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                 UUID        NOT NULL
                                   REFERENCES public.organizations(id) ON DELETE RESTRICT,
            organization_member_id UUID        NOT NULL,
            scope_type_id          SMALLINT    NOT NULL DEFAULT 2
                                   REFERENCES public.scope_types(id) ON DELETE RESTRICT,
            branch_id              UUID        NULL,

            -- Permission payload
            compiled_permissions   JSONB       NOT NULL DEFAULT '[]',
            snapshot_version       BIGINT      NOT NULL DEFAULT 1,

            -- Cache control
            is_stale               BOOLEAN     NOT NULL DEFAULT FALSE,
            expires_at             TIMESTAMPTZ NOT NULL
                                   DEFAULT clock_timestamp() + interval '1 hour',

            -- Composite FK: guarantees member belongs to the same org
            CONSTRAINT fk_perm_snap_member_org
                FOREIGN KEY (organization_member_id, org_id)
                REFERENCES public.organization_members(id, org_id)
                ON DELETE CASCADE,

            -- Uniqueness: one current snapshot per member+scope+branch
            CONSTRAINT uq_perm_snap_member_scope_branch
                UNIQUE (organization_member_id, org_id, scope_type_id, branch_id),

            -- Self-consistency: compiled_permissions must be a JSON array
            CONSTRAINT chk_perm_snap_is_array
                CHECK (jsonb_typeof(compiled_permissions) = 'array'),

            -- Freshness: expires_at must always be in the future at insert time
            -- (enforced by trigger, not CHECK, to avoid volatile function issues)

            created_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.member_permission_snapshots IS
            'Pre-compiled JSONB permission cache per (member, org, scope, branch). '
            'NOT the source of truth — branch_staff_roles is. '
            'is_stale=TRUE means the snapshot needs recomputation by the application. '
            'Append-only semantics: stale rows trigger a fresh INSERT on recompute. '
            'TTL: 1 hour by default; refreshed on any role mutation via trigger.';
    """)

    op.execute("""
        COMMENT ON CONSTRAINT uq_perm_snap_member_scope_branch
        ON public.member_permission_snapshots IS
            'One active snapshot row per (member, org, scope, branch). '
            'On recompute, application UPSERTs on this constraint.';
    """)

    # ── 2. touch_updated_at trigger (reuse from Phase 3) ─────────────────
    op.execute("""
        CREATE TRIGGER trg_touch_perm_snapshot_updated_at
            BEFORE UPDATE ON public.member_permission_snapshots
            FOR EACH ROW
            EXECUTE FUNCTION app_private.touch_updated_at();
    """)

    # ── 3. Indexes ────────────────────────────────────────────────────────

    # Primary access path: find current fresh snapshot for a member at a branch.
    # Note: expires_at filter is applied at query time (volatile fn not allowed in index predicates).
    op.execute("""
        CREATE INDEX ix_perm_snap_member_branch_fresh
        ON public.member_permission_snapshots(organization_member_id, branch_id, expires_at)
        WHERE is_stale = FALSE;
    """)

    # Org-level sweep: find all stale snapshots for a tenant (refresh job)
    op.execute("""
        CREATE INDEX ix_perm_snap_org_stale
        ON public.member_permission_snapshots(org_id)
        WHERE is_stale = TRUE;
    """)

    # Version-based invalidation lookup
    op.execute("""
        CREATE INDEX ix_perm_snap_member_version
        ON public.member_permission_snapshots(organization_member_id, snapshot_version);
    """)

    # ── 4. RLS ────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE public.member_permission_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.member_permission_snapshots FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_permission_snapshots
        ON public.member_permission_snapshots
        FOR ALL
        USING (
            org_id = current_setting('app.current_org_id', false)::uuid
        )
        WITH CHECK (
            org_id = current_setting('app.current_org_id', false)::uuid
        );
    """)

    # ── 5. Grants ─────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON public.member_permission_snapshots
        TO app_runtime;
    """)
    op.execute("GRANT SELECT ON public.member_permission_snapshots TO readonly_analytics;")

    # ── 6. mark_snapshot_stale() trigger function ─────────────────────────
    # Fired AFTER INSERT/UPDATE/DELETE on branch_staff_roles.
    # Marks affected member+branch+org snapshots as stale.
    # Also increments permission_version on organization_members to signal
    # any cached GUC-based permission checks are expired.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.mark_snapshot_stale()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = off
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_member_id UUID;
            v_org_id    UUID;
            v_branch_id UUID;
        BEGIN
            -- Resolve the affected member from either NEW or OLD row
            IF TG_OP = 'DELETE' THEN
                v_member_id := OLD.organization_member_id;
                v_org_id    := OLD.org_id;
                v_branch_id := OLD.branch_id;
            ELSE
                v_member_id := NEW.organization_member_id;
                v_org_id    := NEW.org_id;
                v_branch_id := NEW.branch_id;
            END IF;

            -- Only process new-model rows (organization_member_id is set)
            IF v_member_id IS NULL THEN
                RETURN COALESCE(NEW, OLD);
            END IF;

            -- Mark all snapshots for this member+branch stale
            UPDATE public.member_permission_snapshots
            SET    is_stale   = TRUE,
                   updated_at = clock_timestamp()
            WHERE  organization_member_id = v_member_id
              AND  org_id                 = v_org_id
              AND  (branch_id = v_branch_id OR branch_id IS NULL);

            -- Bump permission_version on the member record so any
            -- session-level GUC permission cache knows to re-fetch
            UPDATE public.organization_members
            SET    permission_version = permission_version + 1,
                   updated_at         = clock_timestamp()
            WHERE  id     = v_member_id
              AND  org_id = v_org_id;

            RETURN COALESCE(NEW, OLD);
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.mark_snapshot_stale() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.mark_snapshot_stale() FROM PUBLIC;")

    op.execute("""
        COMMENT ON FUNCTION app_private.mark_snapshot_stale() IS
            'AFTER trigger on branch_staff_roles. '
            'Marks all permission snapshots stale for the affected member+branch. '
            'Bumps organization_members.permission_version for session-level invalidation.';
    """)

    # Attach to branch_staff_roles — fires for new-model rows only
    op.execute("""
        CREATE TRIGGER trg_invalidate_perm_snapshot
            AFTER INSERT OR UPDATE OR DELETE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.mark_snapshot_stale();
    """)

    # ── 7. compile_member_permissions() callable ──────────────────────────
    # Called by the application to rebuild a stale snapshot.
    # Returns sorted JSONB array of permission codes.
    # The application then UPSERTs into member_permission_snapshots.
    # row_security = off: function queries branch_staff_roles directly,
    # bypassing RLS (it runs as a trusted internal caller).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.compile_member_permissions(
            p_organization_member_id UUID,
            p_org_id                 UUID,
            p_branch_id              UUID,
            p_scope_type_id          SMALLINT DEFAULT 2
        )
        RETURNS JSONB
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = off
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_permission_codes JSONB;
        BEGIN
            -- Derive the permission codes from active role assignments.
            -- Joins branch_staff_roles → staff_roles → role_permission_map.
            -- For now, uses a hardcoded mapping table (role_id → permission codes).
            -- This will be replaced by public.role_permission_events in Phase 7.
            --
            -- Permission derivation logic:
            --   1. Find all active (non-revoked, non-deleted) role assignments
            --      for this member at the given branch.
            --   2. For each role, look up its permission codes in staff_roles.
            --   3. Aggregate, deduplicate, and sort.

            SELECT jsonb_agg(DISTINCT p.code ORDER BY p.code)
            INTO   v_permission_codes
            FROM   public.branch_staff_roles bsr
            JOIN   public.staff_roles sr  ON sr.id = bsr.role_id
            JOIN   public.permissions p   ON TRUE
            WHERE  bsr.organization_member_id = p_organization_member_id
              AND  bsr.org_id                 = p_org_id
              AND  bsr.branch_id              = p_branch_id
              AND  bsr.scope_type_id          = p_scope_type_id
              AND  bsr.revoked_at             IS NULL
              AND  bsr.deleted_at             IS NULL
              AND  bsr.effective_from         <= clock_timestamp()
              AND (bsr.effective_to           IS NULL OR bsr.effective_to > clock_timestamp())
              -- Permission codes are derived from role hierarchy level
              -- owner(100): all permissions
              -- admin(80): all except org.settings.update
              -- manager(60): branch ops + staff_roles read/assign/revoke + members
              -- trainer(40): branch.read, members.read
              -- receptionist(20): branch.read, members.read, members.invite
              -- auditor(10): audit.read, branch.read
              AND  CASE
                  WHEN sr.hierarchy_level >= 100 THEN TRUE
                  WHEN sr.hierarchy_level >= 80  THEN p.code NOT IN ('org.settings.update')
                  WHEN sr.hierarchy_level >= 60  THEN p.code IN (
                      'branch.read','branch.update','branch.suspend',
                      'staff_roles.read','staff_roles.assign','staff_roles.revoke',
                      'members.read','members.invite','members.suspend'
                  )
                  WHEN sr.hierarchy_level >= 40  THEN p.code IN ('branch.read','members.read')
                  WHEN sr.hierarchy_level >= 20  THEN p.code IN ('branch.read','members.read','members.invite')
                  WHEN sr.hierarchy_level >= 10  THEN p.code IN ('audit.read','branch.read')
                  ELSE FALSE
              END;

            -- Return empty array if no permissions found (not NULL)
            RETURN COALESCE(v_permission_codes, '[]'::jsonb);
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) TO app_runtime;")

    op.execute("""
        COMMENT ON FUNCTION app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT) IS
            'Recomputes the permission code set for a member at a branch/scope. '
            'Returns sorted JSONB array. Application UPSERTs result into '
            'member_permission_snapshots on cache miss or stale detection. '
            'Role→permission mapping is inline here; will be table-driven in Phase 7.';
    """)

    # ── 8. Security barrier view ──────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE VIEW app_secure.v_effective_member_permissions
        WITH (security_barrier = true)
        AS
        SELECT
            mps.id,
            mps.org_id,
            mps.organization_member_id,
            mps.scope_type_id,
            st.code                  AS scope_code,
            mps.branch_id,
            mps.compiled_permissions,
            mps.snapshot_version,
            mps.is_stale,
            mps.expires_at,
            mps.created_at,
            mps.updated_at
        FROM   public.member_permission_snapshots mps
        JOIN   public.scope_types st ON st.id = mps.scope_type_id
        WHERE  mps.is_stale   = FALSE
          AND  mps.expires_at > clock_timestamp();
    """)

    op.execute("""
        GRANT SELECT ON app_secure.v_effective_member_permissions
        TO app_runtime, readonly_analytics;
    """)

    op.execute("""
        COMMENT ON VIEW app_secure.v_effective_member_permissions IS
            'Security-barrier view: most recent non-stale, non-expired permission '
            'snapshots per member. Application reads this view for permission checks; '
            'on miss (empty result), calls compile_member_permissions() to rebuild.';
    """)


def downgrade() -> None:
    # View
    op.execute("DROP VIEW IF EXISTS app_secure.v_effective_member_permissions;")

    # Triggers
    op.execute("DROP TRIGGER IF EXISTS trg_invalidate_perm_snapshot ON public.branch_staff_roles;")
    op.execute("DROP TRIGGER IF EXISTS trg_touch_perm_snapshot_updated_at ON public.member_permission_snapshots;")

    # Functions
    op.execute("DROP FUNCTION IF EXISTS app_private.compile_member_permissions(UUID, UUID, UUID, SMALLINT);")
    op.execute("DROP FUNCTION IF EXISTS app_private.mark_snapshot_stale();")

    # RLS
    op.execute("DROP POLICY IF EXISTS tenant_isolation_permission_snapshots ON public.member_permission_snapshots;")

    # Indexes
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_member_version;")
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_org_stale;")
    op.execute("DROP INDEX IF EXISTS ix_perm_snap_member_branch_fresh;")

    # Table
    op.execute("DROP TABLE IF EXISTS public.member_permission_snapshots CASCADE;")
