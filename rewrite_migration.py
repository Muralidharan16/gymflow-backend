import re

header = """\"\"\"add_branch_operating_hours

Revision ID: dbeb400472ec
Revises: dafd2b02005e
Create Date: 2026-05-23 18:11:25.014739

\"\"\"
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dbeb400472ec'
down_revision: Union[str, Sequence[str], None] = 'dafd2b02005e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    \"\"\"Upgrade schema.\"\"\"
"""

downgrade_header = """
def downgrade() -> None:
    \"\"\"Downgrade schema.\"\"\"
"""

upgrades = [
    "ALTER TABLE public.org_branches ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';",
    "COMMENT ON COLUMN public.org_branches.timezone IS 'Strict IANA timezone string defining local wall-clock rules.';",
    "CREATE EXTENSION IF NOT EXISTS btree_gist;",
    """CREATE TABLE public.organization_operating_hours (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        org_id UUID NOT NULL REFERENCES public.organizations(id),
        day_of_week SMALLINT NOT NULL,
        slot_index SMALLINT NOT NULL DEFAULT 1,
        valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
        valid_until DATE,
        open_time TIME, close_time TIME,
        is_closed BOOLEAN NOT NULL DEFAULT FALSE,
        is_24_hours BOOLEAN NOT NULL DEFAULT FALSE,
        is_overnight BOOLEAN GENERATED ALWAYS AS (
            CASE WHEN open_time IS NOT NULL AND close_time IS NOT NULL THEN open_time > close_time ELSE FALSE END
        ) STORED,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        created_by UUID,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_by UUID,
        deleted_at TIMESTAMPTZ,
        CONSTRAINT chk_org_day_of_week CHECK (day_of_week BETWEEN 0 AND 6),
        CONSTRAINT chk_org_times_logic CHECK (
            (is_closed = TRUE AND is_24_hours = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_24_hours = TRUE AND is_closed = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_closed = FALSE AND is_24_hours = FALSE AND open_time IS NOT NULL AND close_time IS NOT NULL AND open_time <> close_time)
        ),
        CONSTRAINT chk_org_validity_dates CHECK (valid_until IS NULL OR valid_until >= valid_from)
    );""",
    "CREATE INDEX ix_org_hours_active ON public.organization_operating_hours (org_id, day_of_week) WHERE deleted_at IS NULL;",
    """ALTER TABLE public.organization_operating_hours
    ADD CONSTRAINT exclude_org_overlapping_validity EXCLUDE USING GIST (
        org_id WITH =, day_of_week WITH =, slot_index WITH =,
        daterange(valid_from, COALESCE(valid_until, 'infinity'::date), '[]') WITH &&
    ) WHERE (deleted_at IS NULL);""",
    """CREATE TABLE public.branch_operating_hours (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        branch_id UUID NOT NULL REFERENCES public.org_branches(id),
        day_of_week SMALLINT NOT NULL, 
        slot_index SMALLINT NOT NULL DEFAULT 1, 
        valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
        valid_until DATE,
        open_time TIME, close_time TIME, 
        is_closed BOOLEAN NOT NULL DEFAULT FALSE,
        is_24_hours BOOLEAN NOT NULL DEFAULT FALSE,
        is_overnight BOOLEAN GENERATED ALWAYS AS (
            CASE WHEN open_time IS NOT NULL AND close_time IS NOT NULL THEN open_time > close_time ELSE FALSE END
        ) STORED,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        created_by UUID,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_by UUID,
        deleted_at TIMESTAMPTZ, 
        CONSTRAINT chk_day_of_week CHECK (day_of_week BETWEEN 0 AND 6),
        CONSTRAINT chk_times_logic CHECK (
            (is_closed = TRUE AND is_24_hours = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_24_hours = TRUE AND is_closed = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_closed = FALSE AND is_24_hours = FALSE AND open_time IS NOT NULL AND close_time IS NOT NULL AND open_time <> close_time)
        ),
        CONSTRAINT chk_validity_dates CHECK (valid_until IS NULL OR valid_until >= valid_from)
    );""",
    "CREATE INDEX ix_branch_hours_active ON public.branch_operating_hours (branch_id, day_of_week) WHERE deleted_at IS NULL;",
    """ALTER TABLE public.branch_operating_hours
    ADD CONSTRAINT exclude_overlapping_validity EXCLUDE USING GIST (
        branch_id WITH =, day_of_week WITH =, slot_index WITH =,
        daterange(valid_from, COALESCE(valid_until, 'infinity'::date), '[]') WITH &&
    ) WHERE (deleted_at IS NULL);""",
    """CREATE TABLE public.branch_special_hours (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        branch_id UUID NOT NULL REFERENCES public.org_branches(id),
        special_date DATE NOT NULL,
        open_time TIME, close_time TIME,
        is_closed BOOLEAN NOT NULL DEFAULT FALSE,
        is_24_hours BOOLEAN NOT NULL DEFAULT FALSE,
        is_overnight BOOLEAN GENERATED ALWAYS AS (
            CASE WHEN open_time IS NOT NULL AND close_time IS NOT NULL THEN open_time > close_time ELSE FALSE END
        ) STORED,
        reason VARCHAR(255),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        created_by UUID,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_by UUID,
        deleted_at TIMESTAMPTZ,
        CONSTRAINT chk_special_times_logic CHECK (
            (is_closed = TRUE AND is_24_hours = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_24_hours = TRUE AND is_closed = FALSE AND open_time IS NULL AND close_time IS NULL) OR
            (is_closed = FALSE AND is_24_hours = FALSE AND open_time IS NOT NULL AND close_time IS NOT NULL AND open_time <> close_time)
        )
    );""",
    "CREATE UNIQUE INDEX uq_branch_special_date_active ON public.branch_special_hours (branch_id, special_date) WHERE deleted_at IS NULL;",
    "CREATE INDEX ix_special_hours_active ON public.branch_special_hours (branch_id, special_date) WHERE deleted_at IS NULL;",
    """CREATE OR REPLACE FUNCTION app_private.role_id(p_system_name TEXT)
    RETURNS INT LANGUAGE SQL STABLE AS $$
      SELECT CASE p_system_name
        WHEN 'manager' THEN 3
        WHEN 'trainer' THEN 4
        WHEN 'receptionist' THEN 5
        WHEN 'auditor' THEN 6
        ELSE NULL
      END;
    $$;""",
    """CREATE OR REPLACE FUNCTION app_private.membership_status_id(p_name TEXT)
    RETURNS INT LANGUAGE SQL STABLE AS $$
      SELECT CASE p_name
        WHEN 'org_admin' THEN 1
        WHEN 'owner' THEN 1
        WHEN 'admin' THEN 2
        WHEN 'active' THEN 3
        ELSE NULL
      END;
    $$;""",
    """CREATE TABLE public.branch_hours_projection (
        branch_id UUID PRIMARY KEY REFERENCES public.org_branches(id),
        projection_version BIGINT NOT NULL DEFAULT 1,
        last_rebuilt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        source_hash TEXT NOT NULL, 
        timezone TEXT NOT NULL,
        current_status VARCHAR(20) NOT NULL CHECK (current_status IN ('OPEN', 'CLOSED', 'HOLIDAY', 'NOT_CONFIGURED')), 
        next_open_at TIMESTAMPTZ,
        next_close_at TIMESTAMPTZ,
        weekly_schedule JSONB NOT NULL,
        upcoming_exceptions JSONB NOT NULL
    );""",
    """CREATE TABLE public.branch_hours_audit_log (
      id UUID DEFAULT gen_random_uuid(),
      table_name TEXT NOT NULL,
      record_id UUID NOT NULL,
      branch_id UUID NOT NULL,
      operation TEXT NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
      changed_by UUID,
      changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      old_data JSONB, new_data JSONB,
      PRIMARY KEY (id, changed_at)
    ) PARTITION BY RANGE (changed_at);""",
    "CREATE INDEX ix_audit_branch ON public.branch_hours_audit_log (branch_id, changed_at DESC);",
    "ALTER TABLE public.organization_operating_hours ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_operating_hours       ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_special_hours         ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_hours_projection      ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_hours_audit_log       ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.organization_operating_hours FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_operating_hours       FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_special_hours         FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_hours_projection      FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_hours_audit_log       FORCE ROW LEVEL SECURITY;",
    """CREATE POLICY tenant_isolation_org_hours ON public.organization_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.organization_members om WHERE om.org_id = organization_operating_hours.org_id
          AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID AND om.deleted_at IS NULL
    ));""",
    """CREATE POLICY tenant_isolation_read_hours ON public.branch_operating_hours FOR SELECT USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""",
    """CREATE POLICY write_branch_hours_org_admin ON public.branch_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.organization_members om JOIN public.org_branches b ON b.org_id = om.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.membership_status_id = app_private.membership_status_id('org_admin')
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""",
    """CREATE POLICY write_branch_hours_manager ON public.branch_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.branch_staff_roles bsr ON bsr.branch_id = b.id
        JOIN public.organization_members om ON om.id = bsr.organization_member_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND bsr.role_id = app_private.role_id('manager')
          AND bsr.revoked_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""",
    """CREATE POLICY tenant_isolation_projection ON public.branch_hours_projection FOR ALL USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_hours_projection.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""",
    """CREATE POLICY tenant_isolation_audit ON public.branch_hours_audit_log FOR SELECT USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_hours_audit_log.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID 
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL
    ));""",
    """CREATE OR REPLACE FUNCTION app_private.audit_branch_hours() RETURNS TRIGGER AS $$
    BEGIN
      INSERT INTO public.branch_hours_audit_log (table_name, record_id, branch_id, operation, changed_by, old_data, new_data)
      VALUES (
        TG_TABLE_NAME, COALESCE(NEW.id, OLD.id), COALESCE(NEW.branch_id, OLD.branch_id), TG_OP, COALESCE(NEW.updated_by, OLD.updated_by),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END
      );
      RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;""",
    "CREATE TRIGGER trg_audit_branch_operating_hours AFTER INSERT OR UPDATE OR DELETE ON public.branch_operating_hours FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours();",
    "CREATE TRIGGER trg_audit_branch_special_hours AFTER INSERT OR UPDATE OR DELETE ON public.branch_special_hours FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours();",
    """CREATE OR REPLACE FUNCTION app_private.cascade_branch_soft_delete() RETURNS TRIGGER AS $$
    BEGIN
      IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        UPDATE public.branch_operating_hours SET deleted_at = NEW.deleted_at WHERE branch_id = NEW.branch_id AND deleted_at IS NULL;
        UPDATE public.branch_special_hours SET deleted_at = NEW.deleted_at WHERE branch_id = NEW.branch_id AND deleted_at IS NULL;
        DELETE FROM public.branch_hours_projection WHERE branch_id = NEW.branch_id;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;""",
    "CREATE TRIGGER trg_cascade_branch_soft_delete AFTER UPDATE OF deleted_at ON public.org_branch_state FOR EACH ROW EXECUTE FUNCTION app_private.cascade_branch_soft_delete();",
    """CREATE OR REPLACE FUNCTION app_private.cascade_org_soft_delete() RETURNS TRIGGER AS $$
    BEGIN
      IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        UPDATE public.organization_operating_hours SET deleted_at = NEW.deleted_at WHERE org_id = NEW.id AND deleted_at IS NULL;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;""",
    "CREATE TRIGGER trg_cascade_org_soft_delete AFTER UPDATE OF deleted_at ON public.organizations FOR EACH ROW EXECUTE FUNCTION app_private.cascade_org_soft_delete();",
    "CREATE TABLE public.branch_hours_audit_log_y2026m05 PARTITION OF public.branch_hours_audit_log FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');",
    "ALTER TABLE public.branch_hours_audit_log_y2026m05 ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE public.branch_hours_audit_log_y2026m05 FORCE ROW LEVEL SECURITY;"
]

downgrades = [
    "DROP TRIGGER IF EXISTS trg_cascade_org_soft_delete ON public.organizations;",
    "DROP TRIGGER IF EXISTS trg_cascade_branch_soft_delete ON public.org_branch_state;",
    "DROP TRIGGER IF EXISTS trg_audit_branch_special_hours ON public.branch_special_hours;",
    "DROP TRIGGER IF EXISTS trg_audit_branch_operating_hours ON public.branch_operating_hours;",
    "DROP FUNCTION IF EXISTS app_private.cascade_org_soft_delete();",
    "DROP FUNCTION IF EXISTS app_private.cascade_branch_soft_delete();",
    "DROP FUNCTION IF EXISTS app_private.audit_branch_hours();",
    "DROP FUNCTION IF EXISTS app_private.membership_status_id(TEXT);",
    "DROP FUNCTION IF EXISTS app_private.role_id(TEXT);",
    "DROP TABLE IF EXISTS public.branch_hours_audit_log CASCADE;",
    "DROP TABLE IF EXISTS public.branch_hours_projection CASCADE;",
    "DROP TABLE IF EXISTS public.branch_special_hours CASCADE;",
    "DROP TABLE IF EXISTS public.branch_operating_hours CASCADE;",
    "DROP TABLE IF EXISTS public.organization_operating_hours CASCADE;",
]

with open('alembic/versions/dbeb400472ec_add_branch_operating_hours.py', 'w') as f:
    f.write(header)
    for q in upgrades:
        f.write(f'    op.execute("""{q}""")\n')
    f.write(downgrade_header)
    for q in downgrades:
        f.write(f'    op.execute("""{q}""")\n')

