"""platform billing phase 2 resolver and shadow projections

Revision ID: f2b3c4d5e6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-16 00:01:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f2b3c4d5e6a7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE_2_TENANT_TABLES = (
    "platform_subscription_changes",
    "platform_access_overrides",
    "platform_entitlement_projection",
    "platform_access_projection",
    "platform_usage_projection",
)


def _enable_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table_name}
        ON public.{table_name}
        FOR ALL
        USING (
            organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        WITH CHECK (
            organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        );
        """
    )


def upgrade() -> None:
    # ── platform_subscription_changes ────────────────────────────────
    op.execute(
        """
        CREATE TABLE public.platform_subscription_changes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            change_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            from_plan_version_id UUID NULL,
            to_plan_version_id UUID NULL,
            from_price_id UUID NULL,
            to_price_id UUID NULL,
            requested_effective_at TIMESTAMPTZ NOT NULL,
            actual_effective_at TIMESTAMPTZ NULL,
            preview_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            request_idempotency_key VARCHAR(160) NOT NULL,
            request_hash CHAR(64) NOT NULL,
            expected_subscription_version BIGINT NOT NULL,
            requested_by UUID NULL,
            canceled_by UUID NULL,
            failure_code VARCHAR(80) NULL,
            failure_detail_safe TEXT NULL,
            version BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_subscription_changes_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_changes_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_changes_from_plan
                FOREIGN KEY (from_plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_changes_to_plan
                FOREIGN KEY (to_plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_changes_from_price
                FOREIGN KEY (from_price_id) REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_changes_to_price
                FOREIGN KEY (to_price_id) REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_subscription_changes_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_subscription_changes_idempotency_key UNIQUE (organization_id, request_idempotency_key),
            CONSTRAINT chk_platform_subscription_changes_change_type CHECK (
                change_type IN ('upgrade', 'downgrade', 'cancel', 'undo_cancel', 'pause', 'resume', 'reactivate')
            ),
            CONSTRAINT chk_platform_subscription_changes_status CHECK (
                status IN ('requested', 'validated', 'provider_pending', 'scheduled', 'applied', 'canceled', 'failed_retryable', 'failed_final')
            ),
            CONSTRAINT chk_platform_subscription_changes_preview_object CHECK (jsonb_typeof(preview_snapshot_json) = 'object'),
            CONSTRAINT chk_platform_subscription_changes_idempotency_key_nonempty CHECK (btrim(request_idempotency_key) <> ''),
            CONSTRAINT chk_platform_subscription_changes_request_hash_format CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_subscription_changes_expected_version_positive CHECK (expected_subscription_version >= 1),
            CONSTRAINT chk_platform_subscription_changes_failure_hash_format CHECK (
                failure_code IS NULL OR failure_code ~ '^[a-zA-Z0-9_-]+$'
            ),
            CONSTRAINT chk_platform_subscription_changes_version_positive CHECK (version >= 1),
            CONSTRAINT chk_platform_subscription_changes_planned_effective CHECK (
                change_type NOT IN ('upgrade', 'downgrade', 'reactivate')
                OR requested_effective_at >= created_at
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_subscription_changes_org_subscription "
        "ON public.platform_subscription_changes (organization_id, subscription_id);"
    )
    op.execute(
        "CREATE INDEX ix_platform_subscription_changes_org_status "
        "ON public.platform_subscription_changes (organization_id, status);"
    )

    # ── platform_access_overrides ────────────────────────────────────
    op.execute(
        """
        CREATE TABLE public.platform_access_overrides (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            override_type TEXT NOT NULL,
            capability_or_feature_key VARCHAR(120) NULL,
            value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            reason_code VARCHAR(80) NOT NULL,
            reason_detail TEXT NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            requested_by UUID NOT NULL,
            approved_by UUID NULL,
            revoked_by UUID NULL,
            revoked_at TIMESTAMPTZ NULL,
            ticket_reference VARCHAR(120) NOT NULL,
            version BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_access_overrides_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_access_overrides_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_access_overrides_override_type CHECK (
                override_type IN ('access_mode', 'entitlement')
            ),
            CONSTRAINT chk_platform_access_overrides_status CHECK (
                status IN ('scheduled', 'active', 'expired', 'revoked')
            ),
            CONSTRAINT chk_platform_access_overrides_expires_after_start CHECK (expires_at > starts_at),
            CONSTRAINT chk_platform_access_overrides_reason_code_nonempty CHECK (btrim(reason_code) <> ''),
            CONSTRAINT chk_platform_access_overrides_reason_detail_nonempty CHECK (btrim(reason_detail) <> ''),
            CONSTRAINT chk_platform_access_overrides_ticket_nonempty CHECK (btrim(ticket_reference) <> ''),
            CONSTRAINT chk_platform_access_overrides_value_object CHECK (jsonb_typeof(value_json) = 'object'),
            CONSTRAINT chk_platform_access_overrides_shape CHECK (
                (
                    override_type = 'access_mode'
                    AND capability_or_feature_key IS NULL
                    AND value_json ? 'mode'
                    AND value_json->>'mode' IN ('full', 'limited_write', 'read_only', 'billing_only', 'blocked')
                )
                OR (
                    override_type = 'entitlement'
                    AND capability_or_feature_key IS NOT NULL
                    AND btrim(capability_or_feature_key) <> ''
                    AND value_json ? 'value_type'
                    AND value_json ? 'value'
                    AND value_json->>'value_type' IN ('boolean', 'integer', 'string', 'json')
                )
            ),
            CONSTRAINT chk_platform_access_overrides_duration_max CHECK (
                expires_at <= starts_at + INTERVAL '30 days'
            ),
            CONSTRAINT chk_platform_access_overrides_normal_duration CHECK (
                approved_by IS NOT NULL OR expires_at <= starts_at + INTERVAL '7 days'
            ),
            CONSTRAINT chk_platform_access_overrides_approval_separation CHECK (
                approved_by IS NULL OR approved_by <> requested_by
            ),
            CONSTRAINT chk_platform_access_overrides_revoked_metadata CHECK (
                status <> 'revoked' OR (revoked_by IS NOT NULL AND revoked_at IS NOT NULL)
            ),
            CONSTRAINT chk_platform_access_overrides_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_access_overrides_org_status "
        "ON public.platform_access_overrides (organization_id, status);"
    )
    op.execute(
        "CREATE INDEX ix_platform_access_overrides_active_window "
        "ON public.platform_access_overrides (organization_id, status, starts_at, expires_at);"
    )

    # ── platform_entitlement_projection ──────────────────────────────
    op.execute(
        """
        CREATE TABLE public.platform_entitlement_projection (
            organization_id UUID NOT NULL,
            feature_key VARCHAR(120) NOT NULL,
            value_type TEXT NOT NULL,
            value_boolean BOOLEAN NULL,
            value_integer BIGINT NULL,
            value_string TEXT NULL,
            value_json JSONB NULL,
            source_plan_version_id UUID NULL,
            source_override_id UUID NULL,
            effective_from TIMESTAMPTZ NOT NULL,
            effective_until TIMESTAMPTZ NULL,
            source_subscription_version BIGINT NOT NULL,
            resolution_version BIGINT NOT NULL,
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            input_sha256 CHAR(64) NOT NULL,
            CONSTRAINT pk_platform_entitlement_projection PRIMARY KEY (organization_id, feature_key),
            CONSTRAINT fk_platform_entitlement_projection_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_entitlement_projection_override_org
                FOREIGN KEY (source_override_id, organization_id)
                REFERENCES public.platform_access_overrides(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT chk_platform_entitlement_projection_value_type CHECK (
                value_type IN ('boolean', 'integer', 'string', 'json')
            ),
            CONSTRAINT chk_platform_entitlement_projection_one_value CHECK (
                num_nonnulls(value_boolean, value_integer, value_string, value_json) = 1
            ),
            CONSTRAINT chk_platform_entitlement_projection_value_matches_type CHECK (
                (value_type = 'boolean' AND value_boolean IS NOT NULL AND value_integer IS NULL AND value_string IS NULL AND value_json IS NULL)
                OR (value_type = 'integer' AND value_boolean IS NULL AND value_integer IS NOT NULL AND value_string IS NULL AND value_json IS NULL)
                OR (value_type = 'string' AND value_boolean IS NULL AND value_integer IS NULL AND value_string IS NOT NULL AND value_json IS NULL)
                OR (value_type = 'json' AND value_boolean IS NULL AND value_integer IS NULL AND value_string IS NULL AND value_json IS NOT NULL)
            ),
            CONSTRAINT chk_platform_entitlement_projection_input_hash CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_entitlement_projection_resolution_version_positive CHECK (resolution_version > 0),
            CONSTRAINT chk_platform_entitlement_projection_source_version_nonneg CHECK (source_subscription_version >= 0),
            CONSTRAINT chk_platform_entitlement_projection_effective_until CHECK (
                effective_until IS NULL OR effective_until > effective_from
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_entitlement_projection_org "
        "ON public.platform_entitlement_projection (organization_id);"
    )

    # ── platform_access_projection ──────────────────────────────────
    op.execute(
        """
        CREATE TABLE public.platform_access_projection (
            organization_id UUID NOT NULL PRIMARY KEY,
            subscription_id UUID NULL,
            mode TEXT NOT NULL,
            reason_code VARCHAR(80) NOT NULL,
            reason_detail_safe TEXT NOT NULL DEFAULT '',
            effective_from TIMESTAMPTZ NOT NULL,
            next_transition_at TIMESTAMPTZ NULL,
            recovery_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_subscription_version BIGINT NULL,
            resolution_version BIGINT NOT NULL,
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            input_sha256 CHAR(64) NOT NULL,
            CONSTRAINT fk_platform_access_projection_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_access_projection_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT chk_platform_access_projection_mode CHECK (
                mode IN ('full', 'limited_write', 'read_only', 'billing_only', 'blocked')
            ),
            CONSTRAINT chk_platform_access_projection_reason_code_nonempty CHECK (btrim(reason_code) <> ''),
            CONSTRAINT chk_platform_access_projection_input_hash CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_access_projection_resolution_version_positive CHECK (resolution_version > 0),
            CONSTRAINT chk_platform_access_projection_source_version_nonneg CHECK (
                source_subscription_version IS NULL OR source_subscription_version >= 0
            ),
            CONSTRAINT chk_platform_access_projection_next_transition CHECK (
                next_transition_at IS NULL OR next_transition_at > effective_from
            ),
            CONSTRAINT chk_platform_access_projection_recovery_actions_array CHECK (jsonb_typeof(recovery_actions_json) = 'array'),
            CONSTRAINT chk_platform_access_projection_recovery_actions_registered CHECK (
                recovery_actions_json <@ '[
                    "VIEW_PLAN_BILLING",
                    "UPDATE_PAYMENT_METHOD",
                    "COMPLETE_PAYMENT_ACTION",
                    "DOWNLOAD_INVOICES",
                    "CONTACT_SUPPORT",
                    "EXPORT_DATA",
                    "UNDO_CANCELLATION"
                ]'::jsonb
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_access_projection_mode "
        "ON public.platform_access_projection (mode);"
    )

    # ── platform_usage_projection ────────────────────────────────────
    op.execute(
        """
        CREATE TABLE public.platform_usage_projection (
            organization_id UUID NOT NULL,
            metric_key VARCHAR(120) NOT NULL,
            current_value BIGINT NOT NULL DEFAULT 0,
            measured_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            source_high_watermark VARCHAR(160) NULL,
            stale_after TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_platform_usage_projection PRIMARY KEY (organization_id, metric_key),
            CONSTRAINT fk_platform_usage_projection_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT chk_platform_usage_projection_current_value_nonneg CHECK (current_value >= 0),
            CONSTRAINT chk_platform_usage_projection_metric_key_nonempty CHECK (btrim(metric_key) <> ''),
            CONSTRAINT chk_platform_usage_projection_stale_after_future CHECK (stale_after >= measured_at)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_usage_projection_org "
        "ON public.platform_usage_projection (organization_id);"
    )

    # ── Updated_at triggers ─────────────────────────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.platform_billing_touch_updated_at()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$;
        """
    )

    for table_name in ("platform_subscription_changes", "platform_access_overrides"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_touch_updated_at
            BEFORE UPDATE ON public.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.platform_billing_touch_updated_at();
            """
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.check_platform_access_override_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status IN ('expired', 'revoked')
               AND NEW.status IN ('scheduled', 'active') THEN
                RAISE EXCEPTION 'platform access override terminal status cannot be reactivated: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_access_overrides_status_transition
        BEFORE UPDATE ON public.platform_access_overrides
        FOR EACH ROW
        EXECUTE FUNCTION public.check_platform_access_override_transition();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.check_platform_subscription_change_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status = NEW.status THEN
                RETURN NEW;
            END IF;

            IF OLD.status IN ('applied', 'canceled', 'failed_final') THEN
                RAISE EXCEPTION 'platform subscription change terminal status cannot transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NOT (
                (OLD.status = 'requested' AND NEW.status IN ('validated', 'canceled', 'failed_retryable', 'failed_final'))
                OR (OLD.status = 'validated' AND NEW.status IN ('provider_pending', 'scheduled', 'applied', 'canceled', 'failed_retryable', 'failed_final'))
                OR (OLD.status = 'provider_pending' AND NEW.status IN ('scheduled', 'applied', 'canceled', 'failed_retryable', 'failed_final'))
                OR (OLD.status = 'scheduled' AND NEW.status IN ('applied', 'canceled', 'failed_retryable', 'failed_final'))
                OR (OLD.status = 'failed_retryable' AND NEW.status IN ('validated', 'provider_pending', 'scheduled', 'failed_final', 'canceled'))
            ) THEN
                RAISE EXCEPTION 'forbidden platform subscription change transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_subscription_changes_status_transition
        BEFORE UPDATE ON public.platform_subscription_changes
        FOR EACH ROW
        EXECUTE FUNCTION public.check_platform_subscription_change_transition();
        """
    )

    # ── Integrity protection for projection tables ────────────────
    # Projection rows are derived and replaceable — the authorized projection
    # service (app.platform_billing.services.projection_service) must be able to
    # INSERT, UPDATE, and DELETE them during transactional refresh.
    #
    # This trigger prevents only source-version regression: once a projection
    # has been computed from a given source subscription version, no UPDATE may
    # set a lower source_subscription_version. This protects against stale data
    # overwriting newer decisions.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.check_platform_projection_source_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.source_subscription_version IS NOT NULL
                   AND OLD.source_subscription_version IS NOT NULL
                   AND NEW.source_subscription_version < OLD.source_subscription_version THEN
                    RAISE EXCEPTION 'projection source version regression: % -> %',
                        OLD.source_subscription_version, NEW.source_subscription_version;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    for proj_table in ("platform_entitlement_projection", "platform_access_projection"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{proj_table}_no_version_regression
            BEFORE UPDATE ON public.{proj_table}
            FOR EACH ROW
            EXECUTE FUNCTION public.check_platform_projection_source_version();
            """
        )

    # RLS is applied to all Phase 2 tenant tables (entitlement_projection, access_projection,
    # usage_projection are tenant-owned; subscription_changes and access_overrides also).
    for table_name in PHASE_2_TENANT_TABLES:
        _enable_rls(table_name)

    # ── Grant permissions to app_runtime ────────────────────────────
    all_phase_2_tables_str = ", ".join(
        f"public.{t}" for t in (
            "platform_subscription_changes",
            "platform_access_overrides",
            "platform_entitlement_projection",
            "platform_access_projection",
            "platform_usage_projection",
        )
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT ON {all_phase_2_tables_str} TO app_runtime;
                GRANT INSERT, UPDATE ON
                    public.platform_entitlement_projection,
                    public.platform_access_projection,
                    public.platform_usage_projection
                TO app_runtime;
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    for table_name in reversed(PHASE_2_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table_name} ON public.{table_name};")

    for proj_table in ("platform_entitlement_projection", "platform_access_projection"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{proj_table}_no_version_regression ON public.{proj_table};")
    op.execute("DROP FUNCTION IF EXISTS public.check_platform_projection_source_version();")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_subscription_changes_status_transition "
        "ON public.platform_subscription_changes;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_access_overrides_status_transition "
        "ON public.platform_access_overrides;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.check_platform_subscription_change_transition();")
    op.execute("DROP FUNCTION IF EXISTS public.check_platform_access_override_transition();")

    for table_name in reversed(("platform_subscription_changes", "platform_access_overrides")):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON public.{table_name};")

    op.execute("DROP TABLE IF EXISTS public.platform_usage_projection;")
    op.execute("DROP TABLE IF EXISTS public.platform_access_projection;")
    op.execute("DROP TABLE IF EXISTS public.platform_entitlement_projection;")
    op.execute("DROP TABLE IF EXISTS public.platform_access_overrides;")
    op.execute("DROP TABLE IF EXISTS public.platform_subscription_changes;")
