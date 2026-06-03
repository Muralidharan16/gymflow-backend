"""RBAC Hardening Phase 7 — Immutable Role-Permission Ledger

Phase 7 of the v18.0 hardening plan.

Creates:
  • public.role_permission_events
      — Immutable append-only ledger for role->permission mappings.
      — event_type: 'grant' or 'revoke'.
      — No updates or deletes allowed (enforced via grants + trigger).

  • public.effective_role_permissions
      — Materialized projection of the ledger (the active permission cache).
      — Used by Phase 6 compile_member_permissions() for fast joins.
      — Includes drift metadata (projected_at, ledger_watermark).

  • app_private.rebuild_effective_role_permissions()
      — Replays the event ledger to rebuild the projection atomically.

  • app_private.raise_ledger_immutable_violation()
      — BEFORE UPDATE/DELETE trigger to protect role_permission_events.

Modifies:
  • app_private.compile_member_permissions()
      — Drops the hardcoded CASE WHEN mapping.
      — Joins against the new effective_role_permissions projection.

Revision ID: 0028_rbac_p7_role_events
Revises: 0027_rbac_p6_perm_snapshots
Create Date: 2026-05-23
"""

from alembic import op

revision = "0028_rbac_p7_role_events"
down_revision = "0027_rbac_p6_perm_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. Immutable Event Ledger ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE public.role_permission_events (
            id            BIGSERIAL   PRIMARY KEY,
            role_id       SMALLINT    NOT NULL
                          REFERENCES public.staff_roles(id) ON DELETE RESTRICT,
            permission_id SMALLINT    NOT NULL
                          REFERENCES public.permissions(id) ON DELETE RESTRICT,
            event_type    VARCHAR(16) NOT NULL
                          CHECK (event_type IN ('grant','revoke')),
            performed_by  UUID        NULL
                          REFERENCES public.organization_users(id) ON DELETE RESTRICT,
            reason_code   VARCHAR(32) NOT NULL DEFAULT 'system.bootstrap',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.role_permission_events IS
            'Immutable append-only ledger for role->permission mappings. '
            'Source of truth for what permissions a role holds at any point in time.';
    """)

    # ── 2. Ledger Immutability Guard ──────────────────────────────────────
    op.execute("REVOKE UPDATE, DELETE ON public.role_permission_events FROM app_runtime;")
    op.execute("GRANT INSERT, SELECT ON public.role_permission_events TO app_runtime;")
    op.execute("GRANT SELECT ON public.role_permission_events TO audit_writer, readonly_analytics;")
    op.execute("GRANT ALL ON public.role_permission_events TO app_security_owner;")
    op.execute("GRANT ALL ON SEQUENCE public.role_permission_events_id_seq TO app_security_owner;")

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.raise_ledger_immutable_violation()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'Security policy violation: Role permission events are immutable. '
                'To change a role, append a new grant/revoke event.'
            USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.raise_ledger_immutable_violation() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.raise_ledger_immutable_violation() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_deny_role_event_mutation
            BEFORE UPDATE OR DELETE ON public.role_permission_events
            FOR EACH ROW
            EXECUTE FUNCTION app_private.raise_ledger_immutable_violation();
    """)

    # ── 3. Projected Cache (Materialized State) ───────────────────────────
    op.execute("""
        CREATE TABLE public.effective_role_permissions (
            role_id           SMALLINT NOT NULL
                              REFERENCES public.staff_roles(id) ON DELETE CASCADE,
            permission_id     SMALLINT NOT NULL
                              REFERENCES public.permissions(id) ON DELETE CASCADE,

            -- Projection metadata for drift detection
            projected_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            projector_version INT NOT NULL DEFAULT 1,
            ledger_watermark  BIGINT NOT NULL,

            PRIMARY KEY (role_id, permission_id)
        );
    """)

    op.execute("""
        COMMENT ON TABLE public.effective_role_permissions IS
            'Materialized projection of role_permission_events. '
            'Read-heavy cache used by RLS and token generation. '
            'Rebuilt automatically when events are appended.';
    """)

    op.execute("ALTER TABLE public.effective_role_permissions OWNER TO app_security_owner;")
    op.execute("GRANT USAGE ON SCHEMA public TO app_security_owner;")
    op.execute("GRANT SELECT ON public.effective_role_permissions TO app_runtime, readonly_analytics;")

    # ── 4. Ledger Replay Function ─────────────────────────────────────────
    # Atomically drops and re-projects the active permissions based on the ledger.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.rebuild_effective_role_permissions()
        RETURNS VOID
        STRICT
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_watermark BIGINT;
        BEGIN
            -- 1. Lock the ledger to ensure a consistent snapshot
            LOCK TABLE public.role_permission_events IN ACCESS SHARE MODE;

            -- 2. Grab the current high-water mark
            SELECT COALESCE(MAX(id), 0) INTO v_watermark
            FROM public.role_permission_events;

            -- 3. Delete old projection completely
            DELETE FROM public.effective_role_permissions;

            -- 4. Replay events in order, taking the LAST event per role+perm
            --    as the final truth (grant or revoke).
            INSERT INTO public.effective_role_permissions (
                role_id,
                permission_id,
                ledger_watermark
            )
            SELECT
                role_id,
                permission_id,
                v_watermark
            FROM (
                SELECT
                    role_id,
                    permission_id,
                    event_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY role_id, permission_id
                        ORDER BY id DESC
                    ) as rn
                FROM public.role_permission_events
                WHERE id <= v_watermark
            ) latest_events
            WHERE latest_events.rn = 1
              AND latest_events.event_type = 'grant';

        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.rebuild_effective_role_permissions() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.rebuild_effective_role_permissions() FROM PUBLIC;")

    # ── 5. Auto-Rebuild Trigger ───────────────────────────────────────────
    # Whenever a new event is appended, rebuild the cache.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.trigger_rebuild_role_permissions()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM app_private.rebuild_effective_role_permissions();
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("ALTER FUNCTION app_private.trigger_rebuild_role_permissions() OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.trigger_rebuild_role_permissions() FROM PUBLIC;")

    op.execute("""
        CREATE TRIGGER trg_auto_rebuild_role_perms
            AFTER INSERT ON public.role_permission_events
            FOR EACH STATEMENT
            EXECUTE FUNCTION app_private.trigger_rebuild_role_permissions();
    """)

    # ── 6. Update Phase 6 compile_member_permissions() ────────────────────
    # Remove the hardcoded CASE logic; use the new effective_role_permissions
    # projection instead.
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
            -- Joins branch_staff_roles → effective_role_permissions → permissions
            SELECT jsonb_agg(DISTINCT p.code ORDER BY p.code)
            INTO   v_permission_codes
            FROM   public.branch_staff_roles bsr
            JOIN   public.effective_role_permissions erp ON erp.role_id = bsr.role_id
            JOIN   public.permissions p                  ON p.id = erp.permission_id
            WHERE  bsr.organization_member_id = p_organization_member_id
              AND  bsr.org_id                 = p_org_id
              AND  bsr.branch_id              = p_branch_id
              AND  bsr.scope_type_id          = p_scope_type_id
              AND  bsr.revoked_at             IS NULL
              AND  bsr.deleted_at             IS NULL
              AND  bsr.effective_from         <= clock_timestamp()
              AND (bsr.effective_to           IS NULL OR bsr.effective_to > clock_timestamp());

            RETURN COALESCE(v_permission_codes, '[]'::jsonb);
        END;
        $$;
    """)

    # ── 7. Seed Initial System Role Permissions ───────────────────────────
    # Since we are removing the hardcoded logic, we must seed the ledger
    # so that the system roles (owner, admin, etc.) still work.

    # 1='owner', 2='admin', 3='manager', 4='trainer', 5='receptionist', 6='auditor'
    op.execute("""
        WITH role_map AS (
            SELECT 1 as r_id, p.id as p_id FROM public.permissions p -- Owner: all
            UNION ALL
            SELECT 2 as r_id, p.id as p_id FROM public.permissions p WHERE p.code != 'org.settings.update' -- Admin: all except org.settings.update
            UNION ALL
            SELECT 3 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN (
                'branch.read','branch.update','branch.suspend',
                'staff_roles.read','staff_roles.assign','staff_roles.revoke',
                'members.read','members.invite','members.suspend'
            )
            UNION ALL
            SELECT 4 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('branch.read','members.read')
            UNION ALL
            SELECT 5 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('branch.read','members.read','members.invite')
            UNION ALL
            SELECT 6 as r_id, p.id as p_id FROM public.permissions p WHERE p.code IN ('audit.read','branch.read')
        )
        INSERT INTO public.role_permission_events (role_id, permission_id, event_type, reason_code)
        SELECT r_id, p_id, 'grant', 'system.bootstrap'
        FROM role_map;
    """)

    # We do not need to call rebuild explicitly, the AFTER INSERT trigger did it!


def downgrade() -> None:
    # 1. Restore compile_member_permissions to hardcoded version
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

            RETURN COALESCE(v_permission_codes, '[]'::jsonb);
        END;
        $$;
    """)

    # 2. Drop triggers and functions
    op.execute("DROP TRIGGER IF EXISTS trg_auto_rebuild_role_perms ON public.role_permission_events;")
    op.execute("DROP FUNCTION IF EXISTS app_private.trigger_rebuild_role_permissions();")
    op.execute("DROP FUNCTION IF EXISTS app_private.rebuild_effective_role_permissions();")
    op.execute("DROP TRIGGER IF EXISTS trg_deny_role_event_mutation ON public.role_permission_events;")
    op.execute("DROP FUNCTION IF EXISTS app_private.raise_ledger_immutable_violation();")

    # 3. Drop tables
    op.execute("DROP TABLE IF EXISTS public.effective_role_permissions CASCADE;")
    op.execute("DROP TABLE IF EXISTS public.role_permission_events CASCADE;")
