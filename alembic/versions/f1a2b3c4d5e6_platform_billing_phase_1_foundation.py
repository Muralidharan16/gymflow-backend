"""platform billing phase 1 foundation

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES = (
    "platform_billing_accounts",
    "platform_subscriptions",
    "platform_subscription_items",
    "platform_subscription_periods",
    "platform_subscription_events",
    "platform_billing_audit_events",
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
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")

    op.execute(
        """
        CREATE TABLE public.platform_products (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(40) NOT NULL,
            name VARCHAR(120) NOT NULL,
            description TEXT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_products_code UNIQUE (code),
            CONSTRAINT chk_platform_products_code_upper CHECK (code = upper(code)),
            CONSTRAINT chk_platform_products_code_nonempty CHECK (btrim(code) <> ''),
            CONSTRAINT chk_platform_products_name_nonempty CHECK (btrim(name) <> ''),
            CONSTRAINT chk_platform_products_status CHECK (status IN ('draft', 'active', 'retired'))
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_policy_versions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(60) NOT NULL,
            policy_type TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'draft',
            payload_sha256 CHAR(64) NULL,
            published_at TIMESTAMPTZ NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_policy_versions_code UNIQUE (code),
            CONSTRAINT uq_platform_policy_versions_type_version UNIQUE (policy_type, version),
            CONSTRAINT chk_platform_policy_versions_code_nonempty CHECK (btrim(code) <> ''),
            CONSTRAINT chk_platform_policy_versions_policy_type CHECK (
                policy_type IN ('trial', 'dunning', 'cancellation', 'downgrade', 'refund', 'retention')
            ),
            CONSTRAINT chk_platform_policy_versions_version_positive CHECK (version > 0),
            CONSTRAINT chk_platform_policy_versions_payload_object CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT chk_platform_policy_versions_status CHECK (status IN ('draft', 'published', 'retired')),
            CONSTRAINT chk_platform_policy_versions_publish_metadata CHECK (
                status = 'draft'
                OR (payload_sha256 IS NOT NULL AND payload_sha256 ~ '^[0-9a-f]{64}$' AND published_at IS NOT NULL)
            )
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_plan_versions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id UUID NOT NULL,
            version INTEGER NOT NULL,
            code VARCHAR(60) NOT NULL,
            display_name VARCHAR(120) NOT NULL,
            description TEXT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            trial_policy_version_id UUID NULL,
            dunning_policy_version_id UUID NULL,
            cancellation_policy_version_id UUID NULL,
            downgrade_policy_version_id UUID NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            published_at TIMESTAMPTZ NULL,
            retired_at TIMESTAMPTZ NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_plan_versions_product
                FOREIGN KEY (product_id) REFERENCES public.platform_products(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_plan_versions_trial_policy
                FOREIGN KEY (trial_policy_version_id) REFERENCES public.platform_policy_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_plan_versions_dunning_policy
                FOREIGN KEY (dunning_policy_version_id) REFERENCES public.platform_policy_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_plan_versions_cancellation_policy
                FOREIGN KEY (cancellation_policy_version_id) REFERENCES public.platform_policy_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_plan_versions_downgrade_policy
                FOREIGN KEY (downgrade_policy_version_id) REFERENCES public.platform_policy_versions(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_plan_versions_code UNIQUE (code),
            CONSTRAINT uq_platform_plan_versions_product_version UNIQUE (product_id, version),
            CONSTRAINT chk_platform_plan_versions_version_positive CHECK (version > 0),
            CONSTRAINT chk_platform_plan_versions_code_nonempty CHECK (btrim(code) <> ''),
            CONSTRAINT chk_platform_plan_versions_display_name_nonempty CHECK (btrim(display_name) <> ''),
            CONSTRAINT chk_platform_plan_versions_status CHECK (status IN ('draft', 'published', 'retired')),
            CONSTRAINT chk_platform_plan_versions_metadata_object CHECK (jsonb_typeof(metadata_json) = 'object'),
            CONSTRAINT chk_platform_plan_versions_publish_metadata CHECK (
                status = 'draft'
                OR (
                    published_at IS NOT NULL
                    AND dunning_policy_version_id IS NOT NULL
                    AND cancellation_policy_version_id IS NOT NULL
                    AND downgrade_policy_version_id IS NOT NULL
                )
            ),
            CONSTRAINT chk_platform_plan_versions_retired_at CHECK (status <> 'retired' OR retired_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_plan_versions_product_status ON public.platform_plan_versions (product_id, status);")

    op.execute(
        """
        CREATE TABLE public.platform_prices (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_version_id UUID NOT NULL,
            code VARCHAR(80) NOT NULL,
            currency_code CHAR(3) NOT NULL,
            country_code CHAR(2) NULL,
            billing_interval TEXT NOT NULL,
            interval_count SMALLINT NOT NULL,
            amount_minor BIGINT NOT NULL,
            tax_behavior TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            valid_from TIMESTAMPTZ NULL,
            valid_until TIMESTAMPTZ NULL,
            provider_price_hint VARCHAR(120) NULL,
            published_at TIMESTAMPTZ NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_prices_plan_version
                FOREIGN KEY (plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_prices_code UNIQUE (code),
            CONSTRAINT chk_platform_prices_code_nonempty CHECK (btrim(code) <> ''),
            CONSTRAINT chk_platform_prices_currency_upper CHECK (currency_code = upper(currency_code)),
            CONSTRAINT chk_platform_prices_currency_format CHECK (currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_prices_country_upper CHECK (country_code IS NULL OR country_code = upper(country_code)),
            CONSTRAINT chk_platform_prices_country_format CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT chk_platform_prices_billing_interval CHECK (billing_interval IN ('month', 'year', 'one_time')),
            CONSTRAINT chk_platform_prices_interval_count_positive CHECK (interval_count > 0),
            CONSTRAINT chk_platform_prices_one_time_interval_count CHECK (
                billing_interval <> 'one_time' OR interval_count = 1
            ),
            CONSTRAINT chk_platform_prices_amount_nonnegative CHECK (amount_minor >= 0),
            CONSTRAINT chk_platform_prices_tax_behavior CHECK (tax_behavior IN ('exclusive', 'inclusive', 'not_applicable')),
            CONSTRAINT chk_platform_prices_status CHECK (status IN ('draft', 'active', 'retired')),
            CONSTRAINT chk_platform_prices_valid_window CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from),
            CONSTRAINT chk_platform_prices_active_metadata CHECK (
                status = 'draft' OR (valid_from IS NOT NULL AND published_at IS NOT NULL)
            )
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_prices_plan_status ON public.platform_prices (plan_version_id, status);")
    op.execute("CREATE INDEX ix_platform_prices_availability ON public.platform_prices (country_code, currency_code, billing_interval);")

    op.execute(
        """
        CREATE TABLE public.platform_feature_definitions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            key VARCHAR(120) NOT NULL,
            display_name VARCHAR(120) NOT NULL,
            value_type TEXT NOT NULL,
            enforcement_mode TEXT NOT NULL,
            unit VARCHAR(40) NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_feature_definitions_key UNIQUE (key),
            CONSTRAINT chk_platform_feature_definitions_key_format CHECK (
                key ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'
            ),
            CONSTRAINT chk_platform_feature_definitions_display_name_nonempty CHECK (btrim(display_name) <> ''),
            CONSTRAINT chk_platform_feature_definitions_description_nonempty CHECK (btrim(description) <> ''),
            CONSTRAINT chk_platform_feature_definitions_value_type CHECK (value_type IN ('boolean', 'integer', 'string', 'json')),
            CONSTRAINT chk_platform_feature_definitions_enforcement_mode CHECK (
                enforcement_mode IN ('hard', 'soft', 'metered', 'informational')
            ),
            CONSTRAINT chk_platform_feature_definitions_status CHECK (status IN ('active', 'retired'))
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_plan_entitlements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_version_id UUID NOT NULL,
            feature_definition_id UUID NOT NULL,
            value_type TEXT NOT NULL,
            value_boolean BOOLEAN NULL,
            value_integer BIGINT NULL,
            value_string TEXT NULL,
            value_json JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_plan_entitlements_plan_version
                FOREIGN KEY (plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_plan_entitlements_feature_definition
                FOREIGN KEY (feature_definition_id) REFERENCES public.platform_feature_definitions(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_plan_entitlements_plan_feature UNIQUE (plan_version_id, feature_definition_id),
            CONSTRAINT chk_platform_plan_entitlements_value_type CHECK (value_type IN ('boolean', 'integer', 'string', 'json')),
            CONSTRAINT chk_platform_plan_entitlements_one_value CHECK (
                num_nonnulls(value_boolean, value_integer, value_string, value_json) = 1
            ),
            CONSTRAINT chk_platform_plan_entitlements_value_matches_type CHECK (
                (value_type = 'boolean' AND value_boolean IS NOT NULL AND value_integer IS NULL AND value_string IS NULL AND value_json IS NULL)
                OR (value_type = 'integer' AND value_boolean IS NULL AND value_integer IS NOT NULL AND value_string IS NULL AND value_json IS NULL)
                OR (value_type = 'string' AND value_boolean IS NULL AND value_integer IS NULL AND value_string IS NOT NULL AND value_json IS NULL)
                OR (value_type = 'json' AND value_boolean IS NULL AND value_integer IS NULL AND value_string IS NULL AND value_json IS NOT NULL)
            )
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_billing_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            legal_name VARCHAR(200) NOT NULL,
            billing_email VARCHAR(320) NOT NULL,
            billing_phone_e164 VARCHAR(20) NULL,
            country_code CHAR(2) NOT NULL,
            default_currency_code CHAR(3) NOT NULL,
            address_line1 TEXT NULL,
            address_line2 TEXT NULL,
            city VARCHAR(120) NOT NULL,
            subdivision VARCHAR(120) NULL,
            postal_code VARCHAR(32) NULL,
            tax_registration_type VARCHAR(30) NULL,
            tax_registration_encrypted TEXT NULL,
            tax_registration_masked VARCHAR(40) NULL,
            tax_registration_hash CHAR(64) NULL,
            tax_verified BOOLEAN NOT NULL DEFAULT false,
            tax_verified_at TIMESTAMPTZ NULL,
            invoice_locale VARCHAR(20) NOT NULL DEFAULT 'en-IN',
            created_by UUID NULL,
            updated_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_billing_accounts_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_billing_accounts_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_billing_accounts_status CHECK (status IN ('active', 'closed')),
            CONSTRAINT chk_platform_billing_accounts_legal_name_nonempty CHECK (btrim(legal_name) <> ''),
            CONSTRAINT chk_platform_billing_accounts_email_basic CHECK (billing_email = lower(billing_email) AND position('@' in billing_email) > 1),
            CONSTRAINT chk_platform_billing_accounts_phone_e164 CHECK (billing_phone_e164 IS NULL OR billing_phone_e164 ~ '^\\+[1-9][0-9]{1,14}$'),
            CONSTRAINT chk_platform_billing_accounts_country_format CHECK (country_code = upper(country_code) AND country_code ~ '^[A-Z]{2}$'),
            CONSTRAINT chk_platform_billing_accounts_currency_format CHECK (default_currency_code = upper(default_currency_code) AND default_currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_billing_accounts_city_nonempty CHECK (btrim(city) <> ''),
            CONSTRAINT chk_platform_billing_accounts_tax_hash_format CHECK (tax_registration_hash IS NULL OR tax_registration_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_billing_accounts_tax_verified_metadata CHECK (tax_verified = false OR tax_verified_at IS NOT NULL),
            CONSTRAINT chk_platform_billing_accounts_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_billing_accounts_one_active_per_org
        ON public.platform_billing_accounts (organization_id)
        WHERE status = 'active';
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            billing_account_id UUID NOT NULL,
            status TEXT NOT NULL,
            current_plan_version_id UUID NOT NULL,
            current_price_id UUID NULL,
            policy_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL,
            current_period_start TIMESTAMPTZ NOT NULL,
            current_period_end TIMESTAMPTZ NOT NULL,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
            cancellation_requested_at TIMESTAMPTZ NULL,
            cancellation_effective_at TIMESTAMPTZ NULL,
            canceled_at TIMESTAMPTZ NULL,
            ended_at TIMESTAMPTZ NULL,
            provider_subscription_mapping_id UUID NULL,
            created_by UUID NULL,
            updated_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_subscriptions_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscriptions_billing_account_org
                FOREIGN KEY (billing_account_id, organization_id)
                REFERENCES public.platform_billing_accounts(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscriptions_plan_version
                FOREIGN KEY (current_plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscriptions_price
                FOREIGN KEY (current_price_id) REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_subscriptions_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_subscriptions_status CHECK (
                status IN ('trialing', 'active', 'past_due', 'pause_scheduled', 'paused', 'cancel_scheduled', 'canceled', 'expired')
            ),
            CONSTRAINT chk_platform_subscriptions_policy_snapshot_object CHECK (jsonb_typeof(policy_snapshot_json) = 'object'),
            CONSTRAINT chk_platform_subscriptions_period_order CHECK (current_period_end > current_period_start),
            CONSTRAINT chk_platform_subscriptions_cancel_metadata CHECK (
                (cancel_at_period_end = false AND cancellation_requested_at IS NULL AND cancellation_effective_at IS NULL)
                OR (cancel_at_period_end = true AND cancellation_requested_at IS NOT NULL AND cancellation_effective_at IS NOT NULL)
            ),
            CONSTRAINT chk_platform_subscriptions_cancel_order CHECK (
                cancellation_requested_at IS NULL
                OR cancellation_effective_at IS NULL
                OR cancellation_effective_at >= cancellation_requested_at
            ),
            CONSTRAINT chk_platform_subscriptions_terminal_metadata CHECK (
                status NOT IN ('canceled', 'expired') OR ended_at IS NOT NULL
            ),
            CONSTRAINT chk_platform_subscriptions_canceled_metadata CHECK (status <> 'canceled' OR canceled_at IS NOT NULL),
            CONSTRAINT chk_platform_subscriptions_terminal_not_current CHECK (
                status NOT IN ('canceled', 'expired') OR cancel_at_period_end = false
            ),
            CONSTRAINT chk_platform_subscriptions_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_subscriptions_org_status ON public.platform_subscriptions (organization_id, status);")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_subscriptions_one_current_per_org
        ON public.platform_subscriptions (organization_id)
        WHERE status IN ('trialing', 'active', 'past_due', 'pause_scheduled', 'paused', 'cancel_scheduled');
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_subscription_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            item_type TEXT NOT NULL,
            plan_version_id UUID NOT NULL,
            price_id UUID NULL,
            quantity INTEGER NOT NULL,
            effective_from TIMESTAMPTZ NOT NULL,
            effective_until TIMESTAMPTZ NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_subscription_items_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_items_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_items_plan_version
                FOREIGN KEY (plan_version_id) REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_items_price
                FOREIGN KEY (price_id) REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_subscription_items_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_subscription_items_item_type CHECK (item_type IN ('base_plan', 'addon')),
            CONSTRAINT chk_platform_subscription_items_quantity_positive CHECK (quantity > 0),
            CONSTRAINT chk_platform_subscription_items_effective_order CHECK (effective_until IS NULL OR effective_until > effective_from),
            CONSTRAINT chk_platform_subscription_items_status CHECK (status IN ('scheduled', 'active', 'ended')),
            CONSTRAINT chk_platform_subscription_items_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_subscription_items_org_subscription ON public.platform_subscription_items (organization_id, subscription_id);")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_subscription_items_one_active_base_plan
        ON public.platform_subscription_items (subscription_id)
        WHERE item_type = 'base_plan' AND status = 'active';
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_subscription_periods (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            period_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ NOT NULL,
            source_invoice_id UUID NULL,
            source_change_id UUID NULL,
            source_override_id UUID NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_subscription_periods_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_periods_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_subscription_periods_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_subscription_periods_period_type CHECK (
                period_type IN ('trial', 'paid', 'grace', 'extension', 'post_cancel_read_only')
            ),
            CONSTRAINT chk_platform_subscription_periods_status CHECK (status IN ('scheduled', 'open', 'closed', 'void')),
            CONSTRAINT chk_platform_subscription_periods_order CHECK (ends_at > starts_at),
            CONSTRAINT chk_platform_subscription_periods_metadata_object CHECK (jsonb_typeof(metadata_json) = 'object')
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_subscription_periods_org_subscription ON public.platform_subscription_periods (organization_id, subscription_id);")
    op.execute(
        """
        ALTER TABLE public.platform_subscription_periods
        ADD CONSTRAINT ex_platform_subscription_periods_no_overlap
        EXCLUDE USING gist (
            subscription_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        WHERE (status <> 'void');
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_subscription_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            sequence_number BIGINT NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            actor_type TEXT NOT NULL,
            actor_id UUID NULL,
            source_type TEXT NOT NULL,
            source_id UUID NULL,
            evidence_sha256 CHAR(64) NULL,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            payload_sha256 CHAR(64) NOT NULL,
            CONSTRAINT fk_platform_subscription_events_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_subscription_events_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_subscription_events_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_subscription_events_subscription_sequence UNIQUE (subscription_id, sequence_number),
            CONSTRAINT chk_platform_subscription_events_sequence_positive CHECK (sequence_number > 0),
            CONSTRAINT chk_platform_subscription_events_event_type_nonempty CHECK (btrim(event_type) <> ''),
            CONSTRAINT chk_platform_subscription_events_actor_type CHECK (actor_type IN ('user', 'system', 'provider', 'support')),
            CONSTRAINT chk_platform_subscription_events_source_type CHECK (
                source_type IN ('command', 'webhook', 'reconciliation', 'scheduler', 'migration')
            ),
            CONSTRAINT chk_platform_subscription_events_evidence_hash CHECK (evidence_sha256 IS NULL OR evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_subscription_events_payload_object CHECK (jsonb_typeof(payload_json) = 'object'),
            CONSTRAINT chk_platform_subscription_events_payload_hash CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_subscription_events_org_subscription ON public.platform_subscription_events (organization_id, subscription_id, sequence_number);")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_subscription_events_source_identity
        ON public.platform_subscription_events (subscription_id, source_type, source_id, event_type)
        WHERE source_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_subscription_events_evidence_identity
        ON public.platform_subscription_events (subscription_id, evidence_sha256, event_type)
        WHERE evidence_sha256 IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_billing_audit_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            organization_id UUID NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id UUID NULL,
            action VARCHAR(120) NOT NULL,
            target_type VARCHAR(120) NOT NULL,
            target_id UUID NULL,
            request_id UUID NULL,
            correlation_id UUID NULL,
            ip_hash CHAR(64) NULL,
            user_agent_hash CHAR(64) NULL,
            before_hash CHAR(64) NULL,
            after_hash CHAR(64) NULL,
            metadata_redacted_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            outcome TEXT NOT NULL,
            reason_code VARCHAR(80) NULL,
            CONSTRAINT fk_platform_billing_audit_events_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_billing_audit_events_id_org UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_billing_audit_events_actor_type CHECK (actor_type IN ('user', 'system', 'provider', 'support')),
            CONSTRAINT chk_platform_billing_audit_events_action_nonempty CHECK (btrim(action) <> ''),
            CONSTRAINT chk_platform_billing_audit_events_target_type_nonempty CHECK (btrim(target_type) <> ''),
            CONSTRAINT chk_platform_billing_audit_events_hashes CHECK (
                (ip_hash IS NULL OR ip_hash ~ '^[0-9a-f]{64}$')
                AND (user_agent_hash IS NULL OR user_agent_hash ~ '^[0-9a-f]{64}$')
                AND (before_hash IS NULL OR before_hash ~ '^[0-9a-f]{64}$')
                AND (after_hash IS NULL OR after_hash ~ '^[0-9a-f]{64}$')
            ),
            CONSTRAINT chk_platform_billing_audit_events_metadata_object CHECK (jsonb_typeof(metadata_redacted_json) = 'object'),
            CONSTRAINT chk_platform_billing_audit_events_outcome CHECK (outcome IN ('succeeded', 'failed', 'denied', 'noop'))
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_billing_audit_events_org_recorded ON public.platform_billing_audit_events (organization_id, recorded_at DESC);")
    op.execute("CREATE INDEX ix_platform_billing_audit_events_org_target ON public.platform_billing_audit_events (organization_id, target_type, target_id);")

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

    for table_name in (
        "platform_products",
        "platform_plan_versions",
        "platform_billing_accounts",
        "platform_subscriptions",
        "platform_subscription_items",
    ):
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
        CREATE OR REPLACE FUNCTION public.prevent_platform_product_invalid_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('active', 'retired')
                   OR EXISTS (SELECT 1 FROM public.platform_plan_versions pv WHERE pv.product_id = OLD.id) THEN
                    RAISE EXCEPTION 'published platform products are retired, not deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.code IS DISTINCT FROM NEW.code
               AND EXISTS (SELECT 1 FROM public.platform_plan_versions pv WHERE pv.product_id = OLD.id) THEN
                RAISE EXCEPTION 'platform product code is immutable after plan creation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_products_protect_mutation
        BEFORE UPDATE OR DELETE ON public.platform_products
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_platform_product_invalid_mutation();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_platform_policy_version_immutable_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('published', 'retired') THEN
                    RAISE EXCEPTION 'published or retired platform policy versions are immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('published', 'retired') THEN
                RAISE EXCEPTION 'published or retired platform policy versions are immutable';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_policy_versions_immutable
        BEFORE UPDATE OR DELETE ON public.platform_policy_versions
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_platform_policy_version_immutable_update();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_platform_plan_version_immutable_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('published', 'retired') THEN
                    RAISE EXCEPTION 'published or retired platform plan versions are immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('published', 'retired') THEN
                IF OLD.status = 'published'
                   AND NEW.status = 'retired'
                   AND NEW.retired_at IS NOT NULL
                   AND ROW(NEW.id, NEW.product_id, NEW.version, NEW.code, NEW.display_name, NEW.description,
                           NEW.trial_policy_version_id, NEW.dunning_policy_version_id,
                           NEW.cancellation_policy_version_id, NEW.downgrade_policy_version_id,
                           NEW.metadata_json, NEW.published_at, NEW.created_by, NEW.created_at)
                       IS NOT DISTINCT FROM
                       ROW(OLD.id, OLD.product_id, OLD.version, OLD.code, OLD.display_name, OLD.description,
                           OLD.trial_policy_version_id, OLD.dunning_policy_version_id,
                           OLD.cancellation_policy_version_id, OLD.downgrade_policy_version_id,
                           OLD.metadata_json, OLD.published_at, OLD.created_by, OLD.created_at) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'published or retired platform plan versions are immutable except retirement metadata';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_plan_versions_immutable
        BEFORE UPDATE OR DELETE ON public.platform_plan_versions
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_platform_plan_version_immutable_update();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_platform_price_immutable_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('active', 'retired') THEN
                    RAISE EXCEPTION 'active or retired platform prices are immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('active', 'retired') THEN
                IF OLD.status = 'active'
                   AND NEW.status = 'retired'
                   AND ROW(NEW.id, NEW.plan_version_id, NEW.code, NEW.currency_code, NEW.country_code,
                           NEW.billing_interval, NEW.interval_count, NEW.amount_minor, NEW.tax_behavior,
                           NEW.valid_from, NEW.valid_until, NEW.provider_price_hint, NEW.published_at,
                           NEW.created_by, NEW.created_at)
                       IS NOT DISTINCT FROM
                       ROW(OLD.id, OLD.plan_version_id, OLD.code, OLD.currency_code, OLD.country_code,
                           OLD.billing_interval, OLD.interval_count, OLD.amount_minor, OLD.tax_behavior,
                           OLD.valid_from, OLD.valid_until, OLD.provider_price_hint, OLD.published_at,
                           OLD.created_by, OLD.created_at) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'active or retired platform prices are immutable except retirement status';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_prices_immutable
        BEFORE UPDATE OR DELETE ON public.platform_prices
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_platform_price_immutable_update();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.validate_platform_plan_entitlement()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            feature_type TEXT;
        BEGIN
            SELECT value_type INTO feature_type
            FROM public.platform_feature_definitions
            WHERE id = NEW.feature_definition_id;

            IF feature_type IS NULL THEN
                RAISE EXCEPTION 'platform entitlement feature definition does not exist';
            END IF;

            IF NEW.value_type <> feature_type THEN
                RAISE EXCEPTION 'platform entitlement value type must match feature definition';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_plan_entitlements_validate
        BEFORE INSERT OR UPDATE ON public.platform_plan_entitlements
        FOR EACH ROW
        EXECUTE FUNCTION public.validate_platform_plan_entitlement();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_platform_plan_entitlement_immutable_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_plan_status TEXT;
        BEGIN
            SELECT status INTO old_plan_status
            FROM public.platform_plan_versions
            WHERE id = OLD.plan_version_id;

            IF old_plan_status IN ('published', 'retired') THEN
                RAISE EXCEPTION 'entitlements attached to published platform plans are immutable';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_plan_entitlements_immutable
        BEFORE UPDATE OR DELETE ON public.platform_plan_entitlements
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_platform_plan_entitlement_immutable_update();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_platform_append_only_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'platform billing append-only rows cannot be updated or deleted';
        END;
        $$;
        """
    )
    for table_name in ("platform_subscription_events", "platform_billing_audit_events"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_append_only
            BEFORE UPDATE OR DELETE ON public.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.prevent_platform_append_only_update();
            """
        )

    for table_name in TENANT_TABLES:
        _enable_rls(table_name)

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT ON
                    public.platform_products,
                    public.platform_policy_versions,
                    public.platform_plan_versions,
                    public.platform_prices,
                    public.platform_feature_definitions,
                    public.platform_plan_entitlements,
                    public.platform_billing_accounts,
                    public.platform_subscriptions,
                    public.platform_subscription_items,
                    public.platform_subscription_periods,
                    public.platform_subscription_events,
                    public.platform_billing_audit_events
                TO app_runtime;
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    for table_name in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table_name} ON public.{table_name};")

    for table_name in ("platform_subscription_events", "platform_billing_audit_events"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON public.{table_name};")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_append_only_update();")

    op.execute("DROP TRIGGER IF EXISTS trg_platform_plan_entitlements_immutable ON public.platform_plan_entitlements;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_plan_entitlement_immutable_update();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_plan_entitlements_validate ON public.platform_plan_entitlements;")
    op.execute("DROP FUNCTION IF EXISTS public.validate_platform_plan_entitlement();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_prices_immutable ON public.platform_prices;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_price_immutable_update();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_plan_versions_immutable ON public.platform_plan_versions;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_plan_version_immutable_update();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_policy_versions_immutable ON public.platform_policy_versions;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_policy_version_immutable_update();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_products_protect_mutation ON public.platform_products;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_platform_product_invalid_mutation();")

    for table_name in (
        "platform_subscription_items",
        "platform_subscriptions",
        "platform_billing_accounts",
        "platform_plan_versions",
        "platform_products",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON public.{table_name};")
    op.execute("DROP FUNCTION IF EXISTS public.platform_billing_touch_updated_at();")

    op.execute("ALTER TABLE public.platform_subscription_periods DROP CONSTRAINT IF EXISTS ex_platform_subscription_periods_no_overlap;")
    op.execute("DROP INDEX IF EXISTS public.ux_platform_subscription_events_evidence_identity;")
    op.execute("DROP INDEX IF EXISTS public.ux_platform_subscription_events_source_identity;")
    op.execute("DROP INDEX IF EXISTS public.ux_platform_subscription_items_one_active_base_plan;")
    op.execute("DROP INDEX IF EXISTS public.ux_platform_subscriptions_one_current_per_org;")
    op.execute("DROP INDEX IF EXISTS public.ux_platform_billing_accounts_one_active_per_org;")

    op.execute("DROP TABLE IF EXISTS public.platform_billing_audit_events;")
    op.execute("DROP TABLE IF EXISTS public.platform_subscription_events;")
    op.execute("DROP TABLE IF EXISTS public.platform_subscription_periods;")
    op.execute("DROP TABLE IF EXISTS public.platform_subscription_items;")
    op.execute("DROP TABLE IF EXISTS public.platform_subscriptions;")
    op.execute("DROP TABLE IF EXISTS public.platform_billing_accounts;")
    op.execute("DROP TABLE IF EXISTS public.platform_plan_entitlements;")
    op.execute("DROP TABLE IF EXISTS public.platform_feature_definitions;")
    op.execute("DROP TABLE IF EXISTS public.platform_prices;")
    op.execute("DROP TABLE IF EXISTS public.platform_plan_versions;")
    op.execute("DROP TABLE IF EXISTS public.platform_policy_versions;")
    op.execute("DROP TABLE IF EXISTS public.platform_products;")
