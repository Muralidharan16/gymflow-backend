"""add_branch_operating_hours

Revision ID: dbeb400472ec
Revises: dafd2b02005e
Create Date: 2026-05-23 18:11:25.014739

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'dbeb400472ec'
down_revision: Union[str, Sequence[str], None] = 'dafd2b02005e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # org_branches.timezone is predecessor-owned (0005_enterprise_branches).
    # DBEB consumes it and adds documentation, but must neither silently adopt
    # incompatible drift nor claim ownership of the column itself.
    op.execute("""
    DO $$
    DECLARE
      timezone_column record;
    BEGIN
      SELECT
        pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
        attribute.attnotnull AS not_null,
        pg_catalog.pg_get_expr(default_data.adbin, default_data.adrelid, true) AS default_expression,
        pg_catalog.col_description(relation.oid, attribute.attnum) AS comment_text
      INTO timezone_column
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace_data
        ON namespace_data.oid = relation.relnamespace
      JOIN pg_catalog.pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attname = 'timezone'
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      LEFT JOIN pg_catalog.pg_attrdef AS default_data
        ON default_data.adrelid = attribute.attrelid
       AND default_data.adnum = attribute.attnum
      WHERE namespace_data.nspname = 'public'
        AND relation.relname = 'org_branches'
        AND relation.relkind IN ('r', 'p');

      IF NOT FOUND THEN
        RAISE EXCEPTION 'Required predecessor column public.org_branches.timezone is absent';
      END IF;
      IF timezone_column.data_type <> 'character varying(64)'
         OR NOT timezone_column.not_null
         OR timezone_column.default_expression <> '''UTC''::character varying'
         OR timezone_column.comment_text IS NOT NULL THEN
        RAISE EXCEPTION
          'Predecessor public.org_branches.timezone contract drifted: type=%, not_null=%, default=%, comment=%',
          timezone_column.data_type,
          timezone_column.not_null,
          timezone_column.default_expression,
          timezone_column.comment_text;
      END IF;
    END
    $$;
    """)
    op.execute("""COMMENT ON COLUMN public.org_branches.timezone IS 'Strict IANA timezone string defining local wall-clock rules.';""")

    # btree_gist is infrastructure-provisioned. This migration consumes it but
    # must never install or take ownership of it.
    op.execute("""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_extension AS extension_data
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = extension_data.extowner
        WHERE extension_data.extname = 'btree_gist'
          AND owner_role.rolname = 'postgres'
      ) THEN
        RAISE EXCEPTION 'Required infrastructure-owned extension btree_gist is absent or ownership drifted';
      END IF;
    END
    $$;
    """)

    op.execute("""CREATE TABLE public.organization_operating_hours (
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
    );""")
    op.execute("""CREATE INDEX ix_org_hours_active ON public.organization_operating_hours (org_id, day_of_week) WHERE deleted_at IS NULL;""")
    op.execute("""ALTER TABLE public.organization_operating_hours
    ADD CONSTRAINT exclude_org_overlapping_validity EXCLUDE USING GIST (
        org_id WITH =, day_of_week WITH =, slot_index WITH =,
        daterange(valid_from, COALESCE(valid_until, 'infinity'::date), '[]') WITH &&
    ) WHERE (deleted_at IS NULL);""")
    op.execute("""CREATE TABLE public.branch_operating_hours (
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
    );""")
    op.execute("""CREATE INDEX ix_branch_hours_active ON public.branch_operating_hours (branch_id, day_of_week) WHERE deleted_at IS NULL;""")
    op.execute("""ALTER TABLE public.branch_operating_hours
    ADD CONSTRAINT exclude_overlapping_validity EXCLUDE USING GIST (
        branch_id WITH =, day_of_week WITH =, slot_index WITH =,
        daterange(valid_from, COALESCE(valid_until, 'infinity'::date), '[]') WITH &&
    ) WHERE (deleted_at IS NULL);""")
    op.execute("""CREATE TABLE public.branch_special_hours (
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
    );""")
    op.execute("""CREATE UNIQUE INDEX uq_branch_special_date_active ON public.branch_special_hours (branch_id, special_date) WHERE deleted_at IS NULL;""")
    op.execute("""CREATE INDEX ix_special_hours_active ON public.branch_special_hours (branch_id, special_date) WHERE deleted_at IS NULL;""")

    # These helpers are revision-owned. A collision must fail rather than let a
    # CREATE OR REPLACE silently overwrite a predecessor-owned function.
    op.execute("""CREATE FUNCTION app_private.role_id(p_system_name TEXT)
    RETURNS INT LANGUAGE SQL STABLE AS $$
      SELECT CASE p_system_name
        WHEN 'manager' THEN 3
        WHEN 'trainer' THEN 4
        WHEN 'receptionist' THEN 5
        WHEN 'auditor' THEN 6
        ELSE NULL
      END;
    $$;""")
    op.execute("""CREATE FUNCTION app_private.membership_status_id(p_name TEXT)
    RETURNS INT LANGUAGE SQL STABLE AS $$
      SELECT CASE p_name
        WHEN 'org_admin' THEN 1
        WHEN 'owner' THEN 1
        WHEN 'admin' THEN 2
        WHEN 'active' THEN 3
        ELSE NULL
      END;
    $$;""")
    op.execute("""CREATE TABLE public.branch_hours_projection (
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
    );""")
    op.execute("""CREATE TABLE public.branch_hours_audit_log (
      id UUID DEFAULT gen_random_uuid(),
      table_name TEXT NOT NULL,
      record_id UUID NOT NULL,
      branch_id UUID NOT NULL,
      operation TEXT NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
      changed_by UUID,
      changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      old_data JSONB, new_data JSONB,
      PRIMARY KEY (id, changed_at)
    ) PARTITION BY RANGE (changed_at);""")
    op.execute("""CREATE INDEX ix_audit_branch ON public.branch_hours_audit_log (branch_id, changed_at DESC);""")
    op.execute("""ALTER TABLE public.organization_operating_hours ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_operating_hours       ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_special_hours         ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_hours_projection      ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_hours_audit_log       ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.organization_operating_hours FORCE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_operating_hours       FORCE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_special_hours         FORCE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_hours_projection      FORCE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_hours_audit_log       FORCE ROW LEVEL SECURITY;""")
    op.execute("""CREATE POLICY tenant_isolation_org_hours ON public.organization_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.organization_members om WHERE om.org_id = organization_operating_hours.org_id
          AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID AND om.deleted_at IS NULL
    ));""")
    op.execute("""CREATE POLICY tenant_isolation_read_hours ON public.branch_operating_hours FOR SELECT USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""")
    op.execute("""CREATE POLICY write_branch_hours_org_admin ON public.branch_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.organization_members om JOIN public.org_branches b ON b.org_id = om.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND om.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.membership_status_id = app_private.membership_status_id('org_admin')
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""")
    op.execute("""CREATE POLICY write_branch_hours_manager ON public.branch_operating_hours FOR ALL USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.branch_staff_roles bsr ON bsr.branch_id = b.id
        JOIN public.organization_members om ON om.id = bsr.organization_member_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_operating_hours.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND bsr.role_id = app_private.role_id('manager')
          AND bsr.revoked_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""")
    op.execute("""CREATE POLICY tenant_isolation_projection ON public.branch_hours_projection FOR ALL USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_hours_projection.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL AND obs.is_active = TRUE
    ));""")
    op.execute("""CREATE POLICY tenant_isolation_audit ON public.branch_hours_audit_log FOR SELECT USING (EXISTS (
        SELECT 1 FROM public.org_branches b JOIN public.organization_members om ON om.org_id = b.org_id
        JOIN public.org_branch_state obs ON b.id = obs.branch_id
        WHERE b.id = branch_hours_audit_log.branch_id
          AND b.org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID
          AND om.user_id = NULLIF(current_setting('app.current_user_id', true), '')::UUID
          AND om.deleted_at IS NULL AND obs.deleted_at IS NULL
    ));""")
    op.execute("""CREATE FUNCTION app_private.audit_branch_hours() RETURNS TRIGGER AS $$
    BEGIN
      INSERT INTO public.branch_hours_audit_log (table_name, record_id, branch_id, operation, changed_by, old_data, new_data)
      VALUES (
        TG_TABLE_NAME, COALESCE(NEW.id, OLD.id), COALESCE(NEW.branch_id, OLD.branch_id), TG_OP, COALESCE(NEW.updated_by, OLD.updated_by),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END
      );
      RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;""")
    op.execute("""CREATE TRIGGER trg_audit_branch_operating_hours AFTER INSERT OR UPDATE OR DELETE ON public.branch_operating_hours FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours();""")
    op.execute("""CREATE TRIGGER trg_audit_branch_special_hours AFTER INSERT OR UPDATE OR DELETE ON public.branch_special_hours FOR EACH ROW EXECUTE FUNCTION app_private.audit_branch_hours();""")
    op.execute("""CREATE FUNCTION app_private.cascade_branch_soft_delete() RETURNS TRIGGER AS $$
    BEGIN
      IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        UPDATE public.branch_operating_hours SET deleted_at = NEW.deleted_at WHERE branch_id = NEW.branch_id AND deleted_at IS NULL;
        UPDATE public.branch_special_hours SET deleted_at = NEW.deleted_at WHERE branch_id = NEW.branch_id AND deleted_at IS NULL;
        DELETE FROM public.branch_hours_projection WHERE branch_id = NEW.branch_id;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;""")
    op.execute("""CREATE TRIGGER trg_cascade_branch_soft_delete AFTER UPDATE OF deleted_at ON public.org_branch_state FOR EACH ROW EXECUTE FUNCTION app_private.cascade_branch_soft_delete();""")
    op.execute("""CREATE FUNCTION app_private.cascade_org_soft_delete() RETURNS TRIGGER AS $$
    BEGIN
      IF NEW.is_active = FALSE AND OLD.is_active = TRUE THEN
        UPDATE public.organization_operating_hours SET deleted_at = clock_timestamp() WHERE org_id = NEW.id AND deleted_at IS NULL;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;""")
    op.execute("""CREATE TRIGGER trg_cascade_org_soft_delete AFTER UPDATE OF is_active ON public.organizations FOR EACH ROW EXECUTE FUNCTION app_private.cascade_org_soft_delete();""")
    op.execute("""CREATE TABLE public.branch_hours_audit_log_y2026m05 PARTITION OF public.branch_hours_audit_log FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');""")
    op.execute("""ALTER TABLE public.branch_hours_audit_log_y2026m05 ENABLE ROW LEVEL SECURITY;""")
    op.execute("""ALTER TABLE public.branch_hours_audit_log_y2026m05 FORCE ROW LEVEL SECURITY;""")


def downgrade() -> None:
    """Downgrade schema without cascading through unexpected dependencies."""
    # Triggers on predecessor-owned relations must be detached before their
    # revision-owned functions are removed.
    op.execute("""DROP TRIGGER IF EXISTS trg_cascade_org_soft_delete ON public.organizations;""")
    op.execute("""DROP TRIGGER IF EXISTS trg_cascade_branch_soft_delete ON public.org_branch_state;""")
    op.execute("""DROP TRIGGER IF EXISTS trg_audit_branch_special_hours ON public.branch_special_hours;""")
    op.execute("""DROP TRIGGER IF EXISTS trg_audit_branch_operating_hours ON public.branch_operating_hours;""")

    # Drop revision-owned relations before helper functions because the RLS
    # policies on these relations depend on role_id()/membership_status_id().
    # RESTRICT is intentional: an unknown external dependency is migration drift
    # and must block rollback rather than be silently deleted with CASCADE.
    op.execute("""DROP TABLE IF EXISTS public.branch_hours_audit_log_y2026m05;""")
    op.execute("""DROP TABLE IF EXISTS public.branch_hours_audit_log;""")
    op.execute("""DROP TABLE IF EXISTS public.branch_hours_projection;""")
    op.execute("""DROP TABLE IF EXISTS public.branch_special_hours;""")
    op.execute("""DROP TABLE IF EXISTS public.branch_operating_hours;""")
    op.execute("""DROP TABLE IF EXISTS public.organization_operating_hours;""")

    op.execute("""DROP FUNCTION IF EXISTS app_private.cascade_org_soft_delete();""")
    op.execute("""DROP FUNCTION IF EXISTS app_private.cascade_branch_soft_delete();""")
    op.execute("""DROP FUNCTION IF EXISTS app_private.audit_branch_hours();""")
    op.execute("""DROP FUNCTION IF EXISTS app_private.membership_status_id(TEXT);""")
    op.execute("""DROP FUNCTION IF EXISTS app_private.role_id(TEXT);""")

    # Restore the predecessor-owned column's documentation state. The column,
    # type, nullability and default belong to 0005 and must survive DBEB rollback.
    op.execute("""COMMENT ON COLUMN public.org_branches.timezone IS NULL;""")
