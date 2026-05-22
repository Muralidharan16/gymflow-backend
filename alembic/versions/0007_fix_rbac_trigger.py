"""Fix RBAC Trigger for Schema Alignment

Revision ID: 0007_fix_rbac_trigger
Revises: 0006_branch_security_audit
Create Date: 2026-05-22 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_fix_rbac_trigger'
down_revision: Union[str, None] = '0006_branch_security_audit'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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

          -- Query from actual gym_owners table to match Doers schema
          SELECT role::TEXT INTO actor_role FROM gym_owners WHERE id = actor_id AND org_id = OLD.org_id;

          IF actor_role IS NULL THEN
            RAISE EXCEPTION 'Actor % has no membership in org %', actor_id, OLD.org_id;
          END IF;

          IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can soft-delete branches';
          END IF;

          -- admin is used here to match StaffRole
          IF OLD.branch_status IS DISTINCT FROM NEW.branch_status AND actor_role NOT IN ('owner', 'admin') THEN
            RAISE EXCEPTION 'Insufficient privileges: staff cannot change branch status';
          END IF;

          IF OLD.branch_status = 'archived' AND NEW.branch_status = 'active' AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can restore archived branches';
          END IF;
          
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    # Revert to the previous version querying org_memberships
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
