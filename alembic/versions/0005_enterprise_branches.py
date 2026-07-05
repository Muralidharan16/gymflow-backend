"""Enterprise Branch Management

Revision ID: 0005_enterprise_branches
Revises: 0004_google_maps_integration
Create Date: 2026-05-22 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005_enterprise_branches'
down_revision: Union[str, None] = '0004_google_maps_integration'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Ticket 1: Foundation Migrations ---
    
    # 1. Add max_branches to organizations
    op.add_column('organizations', sa.Column('max_branches', sa.Integer(), server_default='10', nullable=False))
    
    # 2. Create org_branches table
    op.create_table(
        'org_branches',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('branch_name', sa.String(length=120), nullable=False),
        sa.Column('branch_code', sa.String(length=50), nullable=False),
        sa.Column('internal_slug', sa.String(length=32), nullable=False),
        sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=False),
        sa.Column('currency_code', sa.String(length=3), server_default='USD', nullable=False),
        sa.Column('region_code', sa.String(length=10), nullable=True),
        sa.Column('country_code', sa.String(length=2), nullable=True),
        sa.Column('address_id', sa.UUID(), nullable=True),
        sa.Column('branch_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('id', 'org_id', name='uq_org_branch_pair'),
        sa.UniqueConstraint('org_id', 'branch_code', name='uq_branch_code_per_org')
    )
    
    op.execute("COMMENT ON CONSTRAINT uq_org_branch_pair ON org_branches IS 'Required for composite FK references from branch_audit_log and org_branch_state. Do not drop.';")
    
    op.create_index('ix_org_branches_org_id', 'org_branches', ['org_id'], postgresql_include=['branch_name', 'branch_code', 'created_at'])
    
    # 3. Create org_branch_state table
    op.create_table(
        'org_branch_state',
        sa.Column('branch_id', sa.UUID(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('branch_status', sa.String(length=30), server_default='active', nullable=False),
        sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('is_public', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('version', sa.BigInteger(), server_default='1', nullable=False),
        sa.Column('search_logical_clock', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('search_epoch_ulid', sa.String(length=26), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('branch_id'),
        sa.ForeignKeyConstraint(['branch_id', 'org_id'], ['org_branches.id', 'org_branches.org_id'], name='fk_branch_state_org'),
        sa.CheckConstraint(
            "branch_status IN ('active', 'inactive', 'suspended', 'under_renovation', 'pending_deletion', 'archived', 'cleanup_failed')",
            name='chk_valid_branch_status'
        )
    )
    
    # Autovacuum & HOT update tuning
    op.execute("ALTER TABLE org_branch_state SET (fillfactor = 85, autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01);")
    
    # 4. Create allowed_branch_transitions
    op.create_table(
        'allowed_branch_transitions',
        sa.Column('from_status', sa.Text(), nullable=False),
        sa.Column('to_status', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('from_status', 'to_status')
    )
    
    op.execute("""
        INSERT INTO allowed_branch_transitions VALUES 
        ('active', 'inactive'), ('active', 'pending_deletion'), 
        ('active', 'suspended'), ('suspended', 'active'),
        ('active', 'under_renovation'), ('under_renovation', 'active'), ('under_renovation', 'inactive'),
        ('pending_deletion', 'archived'), ('cleanup_failed', 'pending_deletion'), ('archived', 'active');
    """)
    
    # 5. Create v_active_org_branches view
    op.execute("""
        CREATE VIEW v_active_org_branches WITH (security_barrier = true) AS
        SELECT 
          b.id, b.org_id, b.branch_name, b.branch_code, b.internal_slug, b.timezone, b.currency_code, b.region_code, b.country_code, b.created_by, b.created_at, b.updated_at,
          s.branch_status, s.is_primary, s.is_active, s.is_public, s.version, s.updated_at AS state_updated_at
        FROM org_branches b JOIN org_branch_state s ON b.id = s.branch_id
        WHERE s.deleted_at IS NULL;
    """)

    # --- Ticket 2: Safety Triggers ---
    
    # trg_set_branches_updated_at
    op.execute("""
        CREATE OR REPLACE FUNCTION set_branches_updated_at() RETURNS trigger AS $$ 
        BEGIN 
            IF ROW(NEW.branch_name, NEW.branch_code, NEW.internal_slug, NEW.timezone, NEW.currency_code, NEW.region_code, NEW.country_code, NEW.address_id, NEW.branch_metadata) IS DISTINCT FROM 
               ROW(OLD.branch_name, OLD.branch_code, OLD.internal_slug, OLD.timezone, OLD.currency_code, OLD.region_code, OLD.country_code, OLD.address_id, OLD.branch_metadata) THEN 
                NEW.updated_at = now(); 
            END IF; 
            RETURN NEW; 
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_set_branches_updated_at 
        BEFORE UPDATE ON org_branches 
        FOR EACH ROW EXECUTE FUNCTION set_branches_updated_at();
    """)
    
    # trg_enforce_max_branches
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_max_branches() RETURNS trigger AS $$
        DECLARE
          current_count INTEGER;
          max_allowed INTEGER;
        BEGIN
          SELECT max_branches INTO max_allowed FROM organizations WHERE id = NEW.org_id FOR UPDATE;  
          SELECT COUNT(*) INTO current_count FROM org_branches WHERE org_id = NEW.org_id;

          IF current_count >= max_allowed THEN
            RAISE EXCEPTION 'Organization has reached its maximum branch limit (%)' , max_allowed;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_enforce_max_branches 
        BEFORE INSERT ON org_branches 
        FOR EACH ROW EXECUTE FUNCTION enforce_max_branches();
    """)
    
    # trg_prevent_critical_branch_deletion
    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_critical_branch_deletion() RETURNS trigger AS $$
        DECLARE
          active_count INTEGER;
        BEGIN
          PERFORM 1 FROM organizations WHERE id = OLD.org_id FOR UPDATE;
          SELECT COUNT(*) INTO active_count FROM org_branch_state WHERE org_id = OLD.org_id AND deleted_at IS NULL;

          IF OLD.is_primary = TRUE AND NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
            RAISE EXCEPTION 'Cannot delete the primary branch';
          END IF;
          
          IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL AND active_count <= 1 THEN
            RAISE EXCEPTION 'Cannot delete the last branch of an organization';
          END IF;
          
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_prevent_critical_branch_deletion 
        BEFORE UPDATE ON org_branch_state 
        FOR EACH ROW EXECUTE FUNCTION prevent_critical_branch_deletion();
    """)
    
    # trg_validate_branch_transition
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_branch_transition() RETURNS trigger AS $$
        BEGIN 
          IF NOT EXISTS (SELECT 1 FROM allowed_branch_transitions WHERE from_status = OLD.branch_status AND to_status = NEW.branch_status) THEN 
            RAISE EXCEPTION 'Invalid status transition'; 
          END IF; 
          RETURN NEW; 
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_validate_branch_transition 
        BEFORE UPDATE OF branch_status ON org_branch_state 
        FOR EACH ROW WHEN (OLD.branch_status IS DISTINCT FROM NEW.branch_status) 
        EXECUTE FUNCTION validate_branch_transition();
    """)

    # trg_branch_rbac
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_branch_rbac() RETURNS trigger AS $$
        DECLARE
          actor_role TEXT;
          actor_id UUID;
        BEGIN
          actor_id := current_setting('app.current_user_id', true)::UUID;

          IF actor_id IS NULL THEN
            RETURN NEW; 
          END IF;

          -- We assume org_memberships holds roles.
          SELECT role INTO actor_role FROM org_memberships WHERE user_id = actor_id AND org_id = OLD.org_id;

          IF actor_role IS NULL THEN
            RAISE EXCEPTION 'Actor % has no membership in org %', actor_id, OLD.org_id;
          END IF;

          IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can soft-delete branches';
          END IF;

          IF OLD.branch_status IS DISTINCT FROM NEW.branch_status AND actor_role NOT IN ('owner', 'manager') THEN
            RAISE EXCEPTION 'Insufficient privileges: staff cannot change branch status';
          END IF;

          IF OLD.branch_status = 'archived' AND NEW.branch_status = 'active' AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can restore archived branches';
          END IF;
          
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_branch_rbac 
        BEFORE UPDATE ON org_branch_state 
        FOR EACH ROW EXECUTE FUNCTION enforce_branch_rbac();
    """)

    # trg_increment_branch_state_version
    op.execute("""
        CREATE OR REPLACE FUNCTION increment_branch_state_version() RETURNS trigger AS $$
        BEGIN
          NEW.version = OLD.version + 1;
          NEW.search_logical_clock = OLD.search_logical_clock + 1;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_increment_branch_state_version
        BEFORE UPDATE ON org_branch_state
        FOR EACH ROW
        WHEN (OLD.* IS DISTINCT FROM NEW.*)
        EXECUTE FUNCTION increment_branch_state_version();
    """)


def downgrade() -> None:
    # --- Ticket 2 Reversals ---
    op.execute("DROP TRIGGER IF EXISTS trg_increment_branch_state_version ON org_branch_state;")
    op.execute("DROP FUNCTION IF EXISTS increment_branch_state_version();")
    
    op.execute("DROP TRIGGER IF EXISTS trg_branch_rbac ON org_branch_state;")
    op.execute("DROP FUNCTION IF EXISTS enforce_branch_rbac();")
    
    op.execute("DROP TRIGGER IF EXISTS trg_validate_branch_transition ON org_branch_state;")
    op.execute("DROP FUNCTION IF EXISTS validate_branch_transition();")
    
    op.execute("DROP TRIGGER IF EXISTS trg_prevent_critical_branch_deletion ON org_branch_state;")
    op.execute("DROP FUNCTION IF EXISTS prevent_critical_branch_deletion();")
    
    op.execute("DROP TRIGGER IF EXISTS trg_enforce_max_branches ON org_branches;")
    op.execute("DROP FUNCTION IF EXISTS enforce_max_branches();")
    
    op.execute("DROP TRIGGER IF EXISTS trg_set_branches_updated_at ON org_branches;")
    op.execute("DROP FUNCTION IF EXISTS set_branches_updated_at();")

    # --- Ticket 1 Reversals ---
    op.execute("DROP VIEW IF EXISTS v_active_org_branches;")
    op.drop_table('allowed_branch_transitions')
    op.drop_table('org_branch_state')
    op.drop_index('ix_org_branches_org_id', table_name='org_branches')
    op.drop_table('org_branches')
    op.drop_column('organizations', 'max_branches')
