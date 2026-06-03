"""Branch Staff Role Assignment Subsystem

Revision ID: 0021_staff_roles
Revises: 0020_contacts_hardened
Create Date: 2026-05-23

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0021_staff_roles'
down_revision = '0020_contacts_hardened'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Create lookup enum for branch staff roles
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'branch_staff_role_enum') THEN
                CREATE TYPE public.branch_staff_role_enum AS ENUM ('manager', 'trainer', 'receptionist', 'auditor');
            END IF;
        END$$;
    """)

    # 2. Create organization_users table
    op.execute("""
        CREATE TABLE public.organization_users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            name VARCHAR(120) NOT NULL,
            email CITEXT NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            phone VARCHAR(20) NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_verified BOOLEAN NOT NULL DEFAULT FALSE,
            token_version INT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at TIMESTAMPTZ NULL,
            deleted_by UUID NULL,
            CONSTRAINT uq_org_users_email UNIQUE (org_id, email),
            CONSTRAINT uq_org_users_pair UNIQUE (id, org_id)
        );
    """)

    # 3. Create branch_staff_roles table
    op.execute("""
        CREATE TABLE public.branch_staff_roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            user_id UUID NOT NULL,
            role public.branch_staff_role_enum NOT NULL,
            assigned_by UUID NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            effective_from TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            effective_to TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL,
            revoked_by UUID NULL,
            metadata JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            deleted_at TIMESTAMPTZ NULL,
            deleted_by UUID NULL,
            
            CONSTRAINT chk_temporal_bounds CHECK (effective_to IS NULL OR effective_from < effective_to),
            CONSTRAINT chk_revocation_info CHECK (
                (revoked_at IS NULL AND revoked_by IS NULL) OR
                (revoked_at IS NOT NULL)
            ),
            CONSTRAINT fk_branch_staff_branch_org FOREIGN KEY (branch_id, org_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_branch_staff_user_org FOREIGN KEY (user_id, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_branch_staff_assigned_by FOREIGN KEY (assigned_by, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE SET NULL,
            CONSTRAINT fk_branch_staff_revoked_by FOREIGN KEY (revoked_by, org_id) REFERENCES public.organization_users(id, org_id) ON DELETE SET NULL
        );
    """)

    # 4. Enable btree_gist extension and create overlap exclusion constraint
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT exclude_overlapping_staff_assignments
        EXCLUDE USING gist (
            branch_id WITH =,
            user_id WITH =,
            role WITH =,
            tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz)) WITH &&
        )
        WHERE (deleted_at IS NULL AND revoked_at IS NULL);
    """)

    # 5. Create concurrent and partial indexes
    # Note: Outside transaction blocks, we create indexes using autocommit blocks.
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_org_users_email_lower_active
            ON public.organization_users (email) WHERE (deleted_at IS NULL);
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_branch_staff_user_active
            ON public.branch_staff_roles (user_id, role)
            WHERE (deleted_at IS NULL AND revoked_at IS NULL);
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_branch_staff_branch_active
            ON public.branch_staff_roles (branch_id, role)
            WHERE (deleted_at IS NULL AND revoked_at IS NULL);
        """)

    # 6. Setup RLS Policies
    op.execute("ALTER TABLE public.organization_users ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.organization_users FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.branch_staff_roles FORCE ROW LEVEL SECURITY;")

    op.execute("""
        CREATE POLICY tenant_isolation_org_users ON public.organization_users
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    op.execute("""
        CREATE POLICY tenant_isolation_staff_roles ON public.branch_staff_roles
        FOR ALL
        USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)
        WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID);
    """)

    # 7. Grant Permissions to app_rls_executor and app_user (if roles exist)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.organization_users TO app_rls_executor;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.branch_staff_roles TO app_rls_executor;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.organization_users TO app_user;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.branch_staff_roles TO app_user;")

    # 8. Create trigger functions
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()
        RETURNS TRIGGER 
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.is_active = TRUE AND NEW.is_active = FALSE THEN
                UPDATE public.branch_staff_roles
                SET revoked_at = clock_timestamp(),
                    revoked_by = NULLIF(current_setting('app.current_user_id', true), '')::UUID
                WHERE user_id = NEW.id
                  AND revoked_at IS NULL
                  AND deleted_at IS NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("ALTER FUNCTION app_private.handle_user_deactivation_cascade() OWNER TO app_rls_executor;")

    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()
        RETURNS TRIGGER 
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            current_actor UUID := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            audit_action TEXT;
            audit_diff JSONB;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                audit_action := 'staff_role_assigned';
                audit_diff := jsonb_build_object(
                    'role_assignment_id', NEW.id,
                    'user_id', NEW.user_id,
                    'role', NEW.role,
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
                        'user_id', NEW.user_id,
                        'role', NEW.role,
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
        $$ LANGUAGE plpgsql;
    """)
    op.execute("ALTER FUNCTION app_private.log_branch_staff_role_audit() OWNER TO app_rls_executor;")

    # 9. Create triggers
    op.execute("""
        CREATE TRIGGER trg_user_deactivation_cascade
            AFTER UPDATE OF is_active ON public.organization_users
            FOR EACH ROW
            WHEN (NEW.is_active = FALSE)
            EXECUTE FUNCTION app_private.handle_user_deactivation_cascade();
    """)

    op.execute("""
        CREATE TRIGGER trg_audit_branch_staff_roles
            AFTER INSERT OR UPDATE ON public.branch_staff_roles
            FOR EACH ROW
            EXECUTE FUNCTION app_private.log_branch_staff_role_audit();
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_audit_branch_staff_roles ON public.branch_staff_roles;")
    op.execute("DROP TRIGGER IF EXISTS trg_user_deactivation_cascade ON public.organization_users;")
    
    op.execute("DROP FUNCTION IF EXISTS app_private.log_branch_staff_role_audit();")
    op.execute("DROP FUNCTION IF EXISTS app_private.handle_user_deactivation_cascade();")
    
    op.execute("DROP POLICY IF EXISTS tenant_isolation_staff_roles ON public.branch_staff_roles;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_org_users ON public.organization_users;")
    
    op.execute("DROP TABLE IF EXISTS public.branch_staff_roles CASCADE;")
    op.execute("DROP TABLE IF EXISTS public.organization_users CASCADE;")
    
    op.execute("DROP TYPE IF EXISTS public.branch_staff_role_enum CASCADE;")
