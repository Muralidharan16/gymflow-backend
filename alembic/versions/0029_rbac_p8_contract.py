"""RBAC Hardening Phase 8 — branch_staff_roles Contract Step

Phase 8 (Contract) of the v18.0 hardening plan.

This is the CONTRACT step of the Expand/Contract migration pattern.
It removes legacy columns and enforces constraints on the new ones.

Actions:
  1. Backfill assigned_by and revoked_by to map to organization_members.id instead of user_id.
  2. Drop legacy columns: user_id, role
  3. Validate NOT VALID FK constraints added in Phase 4.
  4. Enforce NOT NULL on organization_member_id and role_id.
  5. Update FK constraints for assigned_by and revoked_by to reference organization_members.

Revision ID: 0029_rbac_p8_contract
Revises: 0028_rbac_p7_role_events
Create Date: 2026-05-23
"""

from alembic import op

revision = "0029_rbac_p8_contract"
down_revision = "0028_rbac_p7_role_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Backfill assigned_by and revoked_by to member IDs ──────────────
    op.execute("""
        UPDATE public.branch_staff_roles bsr
        SET assigned_by = om.id
        FROM public.organization_members om
        WHERE bsr.assigned_by = om.user_id AND bsr.org_id = om.org_id;
    """)

    op.execute("""
        UPDATE public.branch_staff_roles bsr
        SET revoked_by = om.id
        FROM public.organization_members om
        WHERE bsr.revoked_by = om.user_id AND bsr.org_id = om.org_id;
    """)

    # ── 2. Drop old FK constraints on assigned_by / revoked_by ────────────
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_branch_staff_assigned_by;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_branch_staff_revoked_by;")

    # ── 3. Add new FK constraints for assigned_by / revoked_by ────────────
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_assigned_by
        FOREIGN KEY (assigned_by) REFERENCES public.organization_members(id) ON DELETE RESTRICT;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_revoked_by
        FOREIGN KEY (revoked_by) REFERENCES public.organization_members(id) ON DELETE RESTRICT;
    """)

    # ── 4. Drop legacy columns and constraints ────────────────────────────
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_branch_staff_user_org;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS exclude_overlapping_staff_assignments;")
    
    op.execute("DROP INDEX IF EXISTS public.ix_branch_staff_user_active;")
    op.execute("DROP INDEX IF EXISTS public.ix_branch_staff_branch_active;")

    # Also drop the legacy columns
    op.execute("DROP VIEW IF EXISTS app_secure.v_active_branch_staff_roles CASCADE;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS user_id;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP COLUMN IF EXISTS role;")

    # Recreate the security barrier view without legacy columns
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

    # ── 5. Validate Phase 4 NOT VALID constraints ─────────────────────────
    op.execute("ALTER TABLE public.branch_staff_roles VALIDATE CONSTRAINT fk_bsr_member_id;")
    op.execute("ALTER TABLE public.branch_staff_roles VALIDATE CONSTRAINT fk_bsr_member_org;")
    op.execute("ALTER TABLE public.branch_staff_roles VALIDATE CONSTRAINT fk_bsr_role_id;")
    op.execute("ALTER TABLE public.branch_staff_roles VALIDATE CONSTRAINT fk_bsr_scope_type_id;")

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog'
        AS $function$
        DECLARE
            current_actor UUID := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            audit_action TEXT;
            audit_diff JSONB;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                audit_action := 'staff_role_assigned';
                audit_diff := jsonb_build_object(
                    'role_assignment_id', NEW.id,
                    'organization_member_id', NEW.organization_member_id,
                    'role_id', NEW.role_id,
                    'effective_from', NEW.effective_from,
                    'effective_to', NEW.effective_to
                );
                
                INSERT INTO public.branch_audit_log (
                    branch_id, org_id, actor_id, action, reason, diff, created_at
                ) VALUES (
                    NEW.branch_id,
                    NEW.org_id,
                    COALESCE(current_actor, NEW.assigned_by),
                    audit_action,
                    'Staff role assigned to branch',
                    audit_diff,
                    clock_timestamp()
                );
            ELSIF TG_OP = 'UPDATE' THEN
                -- Log revocation
                IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
                    audit_action := 'staff_role_revoked';
                    audit_diff := jsonb_build_object(
                        'role_assignment_id', NEW.id,
                        'organization_member_id', NEW.organization_member_id,
                        'role_id', NEW.role_id,
                        'revoked_at', NEW.revoked_at,
                        'revoked_by', NEW.revoked_by
                    );
                    
                    INSERT INTO public.branch_audit_log (
                        branch_id, org_id, actor_id, action, reason, diff, created_at
                    ) VALUES (
                        NEW.branch_id,
                        NEW.org_id,
                        COALESCE(current_actor, NEW.revoked_by),
                        audit_action,
                        'Staff role assignment revoked',
                        audit_diff,
                        clock_timestamp()
                    );
                END IF;
            END IF;
            RETURN NEW;
        END;
        $function$;
    """)

    # ── 7. Enforce NOT NULL on new tenancy fields ─────────────────────────
    op.execute("ALTER TABLE public.branch_staff_roles ALTER COLUMN organization_member_id SET NOT NULL;")
    op.execute("ALTER TABLE public.branch_staff_roles ALTER COLUMN role_id SET NOT NULL;")


def downgrade() -> None:
    # Downgrade is not trivially supported since data was dropped (user_id, role).
    # In a true expand/contract, contract steps are typically not downgraded without 
    # restoring data from a backup. We provide a schema-only rollback that sets the 
    # columns back to nullable and re-adds the dropped columns.

    op.execute("ALTER TABLE public.branch_staff_roles ALTER COLUMN organization_member_id DROP NOT NULL;")
    op.execute("ALTER TABLE public.branch_staff_roles ALTER COLUMN role_id DROP NOT NULL;")

    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_assigned_by;")
    op.execute("ALTER TABLE public.branch_staff_roles DROP CONSTRAINT IF EXISTS fk_bsr_revoked_by;")

    op.execute("ALTER TABLE public.branch_staff_roles ADD COLUMN user_id UUID NULL;")
    op.execute("ALTER TABLE public.branch_staff_roles ADD COLUMN role VARCHAR(50) NULL;")

    # Warning: data will be NULL. Application must backfill user_id and role 
    # before re-adding strict constraints if downgraded.
