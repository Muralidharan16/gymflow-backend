"""Add branch staff roles deactivation trigger

Revision ID: dafd2b02005e
Revises: b2c3d4e5f6a1
Create Date: 2026-05-23 17:34:34.961439

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dafd2b02005e'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.handle_org_user_deactivation_cascade()
        RETURNS TRIGGER 
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.is_active = TRUE AND NEW.is_active = FALSE THEN
                UPDATE public.branch_staff_roles bsr
                SET revoked_at = clock_timestamp(),
                    revoked_by = NULL
                FROM public.organization_members om
                WHERE bsr.organization_member_id = om.id
                  AND om.user_id = NEW.id
                  AND bsr.revoked_at IS NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("ALTER FUNCTION app_private.handle_org_user_deactivation_cascade() OWNER TO app_rls_executor;")
    op.execute("""
        CREATE TRIGGER trg_org_user_deactivation_cascade
            AFTER UPDATE OF is_active ON public.organization_users
            FOR EACH ROW
            WHEN (NEW.is_active = FALSE)
            EXECUTE FUNCTION app_private.handle_org_user_deactivation_cascade();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS trg_org_user_deactivation_cascade ON public.organization_users;")
    op.execute("DROP FUNCTION IF EXISTS app_private.handle_org_user_deactivation_cascade();")

