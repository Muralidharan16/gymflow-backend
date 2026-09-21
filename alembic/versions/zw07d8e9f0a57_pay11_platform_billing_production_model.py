"""PAY-11 platform billing production model

Revision ID: zw07d8e9f0a57
Revises: zv07d8e9f0a56
Create Date: 2026-09-20 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "zw07d8e9f0a57"
down_revision: Union[str, Sequence[str], None] = "zv07d8e9f0a56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES = (
    "platform_provider_subscriptions",
    "platform_mandates",
    "platform_invoices",
    "platform_invoice_lines",
    "platform_payment_attempts",
    "platform_refunds",
    "platform_credit_notes",
    "platform_credit_note_lines",
)

PAY11_TABLES = (
    "platform_catalog_releases",
    "platform_catalog_release_items",
    "platform_provider_releases",
    "platform_provider_subscriptions",
    "platform_mandates",
    "platform_document_sequences",
    "platform_invoices",
    "platform_invoice_lines",
    "platform_payment_attempts",
    "platform_credit_notes",
    "platform_credit_note_lines",
    "platform_refunds",
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
    op.execute(
        """
        CREATE TABLE public.platform_catalog_releases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(80) NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            manifest_sha256 CHAR(64) NULL,
            published_at TIMESTAMPTZ NULL,
            retired_at TIMESTAMPTZ NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_catalog_releases_code UNIQUE (code),
            CONSTRAINT uq_platform_catalog_releases_version UNIQUE (version),
            CONSTRAINT chk_platform_catalog_releases_version_positive CHECK (version > 0),
            CONSTRAINT chk_platform_catalog_releases_code CHECK (code ~ '^catalog_release_v[1-9][0-9]*$'),
            CONSTRAINT chk_platform_catalog_releases_status CHECK (status IN ('draft', 'published', 'retired')),
            CONSTRAINT chk_platform_catalog_releases_manifest_object CHECK (jsonb_typeof(manifest_json) = 'object'),
            CONSTRAINT chk_platform_catalog_releases_publish_metadata CHECK (
                status = 'draft'
                OR (
                    manifest_sha256 ~ '^[0-9a-f]{64}$'
                    AND published_at IS NOT NULL
                )
            ),
            CONSTRAINT chk_platform_catalog_releases_retired_at CHECK (
                status <> 'retired' OR retired_at IS NOT NULL
            )
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_catalog_release_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            catalog_release_id UUID NOT NULL,
            plan_version_id UUID NOT NULL,
            price_id UUID NULL,
            item_sha256 CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_catalog_release_items_release
                FOREIGN KEY (catalog_release_id)
                REFERENCES public.platform_catalog_releases(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_catalog_release_items_plan
                FOREIGN KEY (plan_version_id)
                REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_catalog_release_items_price
                FOREIGN KEY (price_id)
                REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT chk_platform_catalog_release_items_sha
                CHECK (item_sha256 ~ '^[0-9a-f]{64}$')
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_catalog_release_items_plan_no_price
        ON public.platform_catalog_release_items (catalog_release_id, plan_version_id)
        WHERE price_id IS NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_catalog_release_items_plan_price
        ON public.platform_catalog_release_items (catalog_release_id, plan_version_id, price_id)
        WHERE price_id IS NOT NULL;
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_catalog_release_items_release "
        "ON public.platform_catalog_release_items (catalog_release_id);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_provider_releases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(80) NOT NULL,
            version INTEGER NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            adapter_version VARCHAR(80) NOT NULL,
            contract_sha256 CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            manifest_sha256 CHAR(64) NULL,
            published_at TIMESTAMPTZ NULL,
            retired_at TIMESTAMPTZ NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_provider_releases_code UNIQUE (code),
            CONSTRAINT uq_platform_provider_releases_version UNIQUE (version),
            CONSTRAINT uq_platform_provider_releases_adapter
                UNIQUE (provider_code, environment, adapter_version),
            CONSTRAINT chk_platform_provider_releases_version_positive CHECK (version > 0),
            CONSTRAINT chk_platform_provider_releases_code CHECK (code ~ '^provider_release_v[1-9][0-9]*$'),
            CONSTRAINT chk_platform_provider_releases_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_provider_releases_environment CHECK (environment IN ('test', 'sandbox', 'production')),
            CONSTRAINT chk_platform_provider_releases_adapter_version CHECK (btrim(adapter_version) <> ''),
            CONSTRAINT chk_platform_provider_releases_contract_sha CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_provider_releases_status CHECK (status IN ('draft', 'published', 'retired')),
            CONSTRAINT chk_platform_provider_releases_manifest_object CHECK (jsonb_typeof(manifest_json) = 'object'),
            CONSTRAINT chk_platform_provider_releases_publish_metadata CHECK (
                status = 'draft'
                OR (
                    manifest_sha256 ~ '^[0-9a-f]{64}$'
                    AND published_at IS NOT NULL
                )
            ),
            CONSTRAINT chk_platform_provider_releases_retired_at CHECK (
                status <> 'retired' OR retired_at IS NOT NULL
            )
        );
        """
    )

    op.execute(
        """
        ALTER TABLE public.platform_subscriptions
            ADD COLUMN accepted_catalog_release_id UUID NULL,
            ADD COLUMN accepted_provider_release_id UUID NULL,
            ADD COLUMN accepted_plan_version_id UUID NULL,
            ADD COLUMN accepted_price_id UUID NULL,
            ADD COLUMN commercial_contract_sha256 CHAR(64) NULL,
            ADD COLUMN commercial_contract_accepted_at TIMESTAMPTZ NULL,
            ADD COLUMN commercial_contract_migrated_at TIMESTAMPTZ NULL,
            ADD COLUMN commercial_contract_migration_reason TEXT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE public.platform_subscriptions
            ADD CONSTRAINT fk_platform_subscriptions_accepted_catalog_release
                FOREIGN KEY (accepted_catalog_release_id)
                REFERENCES public.platform_catalog_releases(id) ON DELETE RESTRICT,
            ADD CONSTRAINT fk_platform_subscriptions_accepted_provider_release
                FOREIGN KEY (accepted_provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            ADD CONSTRAINT fk_platform_subscriptions_accepted_plan
                FOREIGN KEY (accepted_plan_version_id)
                REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            ADD CONSTRAINT fk_platform_subscriptions_accepted_price
                FOREIGN KEY (accepted_price_id)
                REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            ADD CONSTRAINT chk_platform_subscriptions_commercial_contract_sha
                CHECK (
                    commercial_contract_sha256 IS NULL
                    OR commercial_contract_sha256 ~ '^[0-9a-f]{64}$'
                ),
            ADD CONSTRAINT chk_platform_subscriptions_commercial_contract_shape
                CHECK (
                    (
                        accepted_catalog_release_id IS NULL
                        AND accepted_provider_release_id IS NULL
                        AND accepted_plan_version_id IS NULL
                        AND accepted_price_id IS NULL
                        AND commercial_contract_sha256 IS NULL
                        AND commercial_contract_accepted_at IS NULL
                    )
                    OR
                    (
                        accepted_catalog_release_id IS NOT NULL
                        AND accepted_plan_version_id IS NOT NULL
                        AND commercial_contract_sha256 IS NOT NULL
                        AND commercial_contract_accepted_at IS NOT NULL
                    )
                ),
            ADD CONSTRAINT chk_platform_subscriptions_contract_migration_metadata
                CHECK (
                    (commercial_contract_migrated_at IS NULL AND commercial_contract_migration_reason IS NULL)
                    OR (
                        commercial_contract_migrated_at IS NOT NULL
                        AND commercial_contract_migration_reason IS NOT NULL
                        AND btrim(commercial_contract_migration_reason) <> ''
                    )
                );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_provider_subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            provider_customer_id UUID NOT NULL,
            provider_release_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            external_subscription_ref VARCHAR(200) NOT NULL,
            status TEXT NOT NULL,
            current_period_start TIMESTAMPTZ NULL,
            current_period_end TIMESTAMPTZ NULL,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
            canceled_at TIMESTAMPTZ NULL,
            provider_evidence_sha256 CHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_provider_subscriptions_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_provider_subscriptions_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_provider_subscriptions_customer_org
                FOREIGN KEY (provider_customer_id, organization_id)
                REFERENCES public.platform_provider_customers(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_provider_subscriptions_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_provider_subscriptions_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_provider_subscriptions_external
                UNIQUE (provider_code, external_subscription_ref),
            CONSTRAINT chk_platform_provider_subscriptions_provider_code
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_provider_subscriptions_external_ref
                CHECK (btrim(external_subscription_ref) <> ''),
            CONSTRAINT chk_platform_provider_subscriptions_status
                CHECK (status IN ('pending', 'trialing', 'active', 'past_due', 'paused', 'canceled', 'expired')),
            CONSTRAINT chk_platform_provider_subscriptions_period_order
                CHECK (
                    current_period_end IS NULL
                    OR current_period_start IS NULL
                    OR current_period_end > current_period_start
                ),
            CONSTRAINT chk_platform_provider_subscriptions_evidence_sha
                CHECK (
                    provider_evidence_sha256 IS NULL
                    OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_provider_subscriptions_version CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_provider_subscriptions_org_subscription "
        "ON public.platform_provider_subscriptions (organization_id, subscription_id);"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_provider_subscriptions_current
        ON public.platform_provider_subscriptions (subscription_id, provider_code)
        WHERE status NOT IN ('canceled', 'expired');
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_mandates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            provider_customer_id UUID NOT NULL,
            provider_subscription_id UUID NULL,
            payment_method_id UUID NULL,
            provider_release_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            external_mandate_ref VARCHAR(200) NOT NULL,
            mandate_type VARCHAR(40) NOT NULL,
            status TEXT NOT NULL,
            currency_code CHAR(3) NULL,
            max_amount_minor BIGINT NULL,
            valid_from TIMESTAMPTZ NULL,
            valid_until TIMESTAMPTZ NULL,
            activated_at TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL,
            provider_evidence_sha256 CHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_mandates_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_mandates_customer_org
                FOREIGN KEY (provider_customer_id, organization_id)
                REFERENCES public.platform_provider_customers(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_mandates_subscription_org
                FOREIGN KEY (provider_subscription_id, organization_id)
                REFERENCES public.platform_provider_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_mandates_payment_method_org
                FOREIGN KEY (payment_method_id, organization_id)
                REFERENCES public.platform_payment_methods(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_mandates_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_mandates_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_mandates_external UNIQUE (provider_code, external_mandate_ref),
            CONSTRAINT chk_platform_mandates_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_mandates_external_ref CHECK (btrim(external_mandate_ref) <> ''),
            CONSTRAINT chk_platform_mandates_type CHECK (btrim(mandate_type) <> ''),
            CONSTRAINT chk_platform_mandates_status
                CHECK (status IN ('pending', 'active', 'revoked', 'expired', 'failed')),
            CONSTRAINT chk_platform_mandates_currency
                CHECK (
                    currency_code IS NULL
                    OR (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$')
                ),
            CONSTRAINT chk_platform_mandates_max_amount CHECK (max_amount_minor IS NULL OR max_amount_minor > 0),
            CONSTRAINT chk_platform_mandates_valid_window
                CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from),
            CONSTRAINT chk_platform_mandates_evidence_sha
                CHECK (
                    provider_evidence_sha256 IS NULL
                    OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_mandates_version CHECK (version >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_mandates_org_status ON public.platform_mandates (organization_id, status);")

    op.execute(
        """
        CREATE TABLE public.platform_document_sequences (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            legal_entity_code VARCHAR(60) NOT NULL,
            document_type TEXT NOT NULL,
            financial_year VARCHAR(12) NOT NULL,
            series_code VARCHAR(40) NOT NULL,
            prefix VARCHAR(40) NOT NULL,
            padding SMALLINT NOT NULL DEFAULT 6,
            last_number BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_document_sequences_identity
                UNIQUE (legal_entity_code, document_type, financial_year, series_code),
            CONSTRAINT chk_platform_document_sequences_type
                CHECK (document_type IN ('invoice', 'credit_note')),
            CONSTRAINT chk_platform_document_sequences_legal_entity CHECK (btrim(legal_entity_code) <> ''),
            CONSTRAINT chk_platform_document_sequences_financial_year CHECK (btrim(financial_year) <> ''),
            CONSTRAINT chk_platform_document_sequences_series CHECK (btrim(series_code) <> ''),
            CONSTRAINT chk_platform_document_sequences_prefix CHECK (btrim(prefix) <> ''),
            CONSTRAINT chk_platform_document_sequences_padding CHECK (padding BETWEEN 1 AND 12),
            CONSTRAINT chk_platform_document_sequences_last_number CHECK (last_number >= 0),
            CONSTRAINT chk_platform_document_sequences_status CHECK (status IN ('active', 'closed'))
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_invoices (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            billing_account_id UUID NOT NULL,
            subscription_id UUID NULL,
            document_sequence_id UUID NULL,
            invoice_number VARCHAR(120) NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            currency_code CHAR(3) NOT NULL,
            subtotal_minor BIGINT NOT NULL,
            tax_minor BIGINT NOT NULL,
            total_minor BIGINT NOT NULL,
            amount_due_minor BIGINT NOT NULL,
            catalog_release_id UUID NULL,
            provider_release_id UUID NULL,
            plan_version_id UUID NULL,
            price_id UUID NULL,
            commercial_contract_sha256 CHAR(64) NULL,
            tax_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            billing_address_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            provider_code VARCHAR(40) NULL,
            provider_invoice_ref VARCHAR(200) NULL,
            artifact_sha256 CHAR(64) NULL,
            artifact_ref VARCHAR(300) NULL,
            issued_at TIMESTAMPTZ NULL,
            due_at TIMESTAMPTZ NULL,
            paid_at TIMESTAMPTZ NULL,
            voided_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_invoices_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_billing_account_org
                FOREIGN KEY (billing_account_id, organization_id)
                REFERENCES public.platform_billing_accounts(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_sequence
                FOREIGN KEY (document_sequence_id)
                REFERENCES public.platform_document_sequences(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_catalog_release
                FOREIGN KEY (catalog_release_id)
                REFERENCES public.platform_catalog_releases(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_provider_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_plan
                FOREIGN KEY (plan_version_id)
                REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoices_price
                FOREIGN KEY (price_id)
                REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_invoices_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_invoices_sequence_number UNIQUE (document_sequence_id, invoice_number),
            CONSTRAINT uq_platform_invoices_provider_ref UNIQUE (provider_code, provider_invoice_ref),
            CONSTRAINT chk_platform_invoices_status
                CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')),
            CONSTRAINT chk_platform_invoices_currency
                CHECK (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_invoices_amounts_nonnegative
                CHECK (subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0 AND amount_due_minor >= 0),
            CONSTRAINT chk_platform_invoices_total_math CHECK (total_minor = subtotal_minor + tax_minor),
            CONSTRAINT chk_platform_invoices_amount_due CHECK (amount_due_minor <= total_minor),
            CONSTRAINT chk_platform_invoices_tax_snapshot CHECK (jsonb_typeof(tax_snapshot_json) = 'object'),
            CONSTRAINT chk_platform_invoices_address_snapshot CHECK (jsonb_typeof(billing_address_snapshot_json) = 'object'),
            CONSTRAINT chk_platform_invoices_contract_sha
                CHECK (
                    commercial_contract_sha256 IS NULL
                    OR commercial_contract_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_invoices_artifact_sha
                CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_invoices_provider_shape
                CHECK (
                    (provider_code IS NULL AND provider_invoice_ref IS NULL)
                    OR (
                        provider_code ~ '^[a-z0-9_]+$'
                        AND provider_invoice_ref IS NOT NULL
                        AND btrim(provider_invoice_ref) <> ''
                    )
                ),
            CONSTRAINT chk_platform_invoices_issued_metadata
                CHECK (
                    status = 'draft'
                    OR (
                        document_sequence_id IS NOT NULL
                        AND invoice_number IS NOT NULL
                        AND btrim(invoice_number) <> ''
                        AND issued_at IS NOT NULL
                        AND catalog_release_id IS NOT NULL
                        AND plan_version_id IS NOT NULL
                        AND commercial_contract_sha256 IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_invoices_paid_metadata
                CHECK (status <> 'paid' OR (paid_at IS NOT NULL AND amount_due_minor = 0)),
            CONSTRAINT chk_platform_invoices_void_metadata
                CHECK (status <> 'void' OR voided_at IS NOT NULL),
            CONSTRAINT chk_platform_invoices_version CHECK (version >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_invoices_org_status ON public.platform_invoices (organization_id, status);")
    op.execute("CREATE INDEX ix_platform_invoices_org_issued ON public.platform_invoices (organization_id, issued_at DESC);")

    op.execute(
        """
        CREATE TABLE public.platform_invoice_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            line_number INTEGER NOT NULL,
            line_type TEXT NOT NULL,
            description TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_amount_minor BIGINT NOT NULL,
            net_amount_minor BIGINT NOT NULL,
            tax_rate_bps INTEGER NOT NULL DEFAULT 0,
            tax_amount_minor BIGINT NOT NULL,
            gross_amount_minor BIGINT NOT NULL,
            tax_code VARCHAR(40) NULL,
            plan_version_id UUID NULL,
            price_id UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_invoice_lines_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoice_lines_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoice_lines_plan
                FOREIGN KEY (plan_version_id)
                REFERENCES public.platform_plan_versions(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_invoice_lines_price
                FOREIGN KEY (price_id)
                REFERENCES public.platform_prices(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_invoice_lines_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_invoice_lines_number UNIQUE (invoice_id, line_number),
            CONSTRAINT chk_platform_invoice_lines_number_positive CHECK (line_number > 0),
            CONSTRAINT chk_platform_invoice_lines_type
                CHECK (line_type IN ('subscription', 'addon', 'discount', 'adjustment', 'tax')),
            CONSTRAINT chk_platform_invoice_lines_description CHECK (btrim(description) <> ''),
            CONSTRAINT chk_platform_invoice_lines_quantity CHECK (quantity > 0),
            CONSTRAINT chk_platform_invoice_lines_amounts
                CHECK (
                    unit_amount_minor >= 0
                    AND net_amount_minor >= 0
                    AND tax_amount_minor >= 0
                    AND gross_amount_minor >= 0
                ),
            CONSTRAINT chk_platform_invoice_lines_tax_rate CHECK (tax_rate_bps BETWEEN 0 AND 10000),
            CONSTRAINT chk_platform_invoice_lines_gross_math CHECK (gross_amount_minor = net_amount_minor + tax_amount_minor)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_invoice_lines_invoice ON public.platform_invoice_lines (invoice_id, line_number);")

    op.execute(
        """
        CREATE TABLE public.platform_credit_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            document_sequence_id UUID NULL,
            credit_note_number VARCHAR(120) NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            reason_code VARCHAR(80) NOT NULL,
            currency_code CHAR(3) NOT NULL,
            subtotal_minor BIGINT NOT NULL,
            tax_minor BIGINT NOT NULL,
            total_minor BIGINT NOT NULL,
            artifact_sha256 CHAR(64) NULL,
            artifact_ref VARCHAR(300) NULL,
            issued_at TIMESTAMPTZ NULL,
            applied_at TIMESTAMPTZ NULL,
            voided_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_credit_notes_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_credit_notes_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_credit_notes_sequence
                FOREIGN KEY (document_sequence_id)
                REFERENCES public.platform_document_sequences(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_credit_notes_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_credit_notes_sequence_number
                UNIQUE (document_sequence_id, credit_note_number),
            CONSTRAINT chk_platform_credit_notes_status
                CHECK (status IN ('draft', 'issued', 'applied', 'void')),
            CONSTRAINT chk_platform_credit_notes_reason CHECK (btrim(reason_code) <> ''),
            CONSTRAINT chk_platform_credit_notes_currency
                CHECK (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_credit_notes_amounts
                CHECK (subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor > 0),
            CONSTRAINT chk_platform_credit_notes_total_math CHECK (total_minor = subtotal_minor + tax_minor),
            CONSTRAINT chk_platform_credit_notes_artifact_sha
                CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_credit_notes_issued_metadata
                CHECK (
                    status = 'draft'
                    OR (
                        document_sequence_id IS NOT NULL
                        AND credit_note_number IS NOT NULL
                        AND btrim(credit_note_number) <> ''
                        AND issued_at IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_credit_notes_applied_metadata
                CHECK (status <> 'applied' OR applied_at IS NOT NULL),
            CONSTRAINT chk_platform_credit_notes_void_metadata
                CHECK (status <> 'void' OR voided_at IS NOT NULL),
            CONSTRAINT chk_platform_credit_notes_version CHECK (version >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_platform_credit_notes_org_invoice ON public.platform_credit_notes (organization_id, invoice_id);")

    op.execute(
        """
        CREATE TABLE public.platform_credit_note_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            credit_note_id UUID NOT NULL,
            invoice_line_id UUID NULL,
            line_number INTEGER NOT NULL,
            line_type TEXT NOT NULL,
            description TEXT NOT NULL,
            net_amount_minor BIGINT NOT NULL,
            tax_amount_minor BIGINT NOT NULL,
            gross_amount_minor BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_credit_note_lines_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_credit_note_lines_note_org
                FOREIGN KEY (credit_note_id, organization_id)
                REFERENCES public.platform_credit_notes(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_credit_note_lines_invoice_line_org
                FOREIGN KEY (invoice_line_id, organization_id)
                REFERENCES public.platform_invoice_lines(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_credit_note_lines_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_credit_note_lines_number UNIQUE (credit_note_id, line_number),
            CONSTRAINT chk_platform_credit_note_lines_number_positive CHECK (line_number > 0),
            CONSTRAINT chk_platform_credit_note_lines_type
                CHECK (line_type IN ('charge_reversal', 'tax_reversal', 'adjustment')),
            CONSTRAINT chk_platform_credit_note_lines_description CHECK (btrim(description) <> ''),
            CONSTRAINT chk_platform_credit_note_lines_amounts
                CHECK (net_amount_minor >= 0 AND tax_amount_minor >= 0 AND gross_amount_minor > 0),
            CONSTRAINT chk_platform_credit_note_lines_gross_math
                CHECK (gross_amount_minor = net_amount_minor + tax_amount_minor)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_credit_note_lines_note "
        "ON public.platform_credit_note_lines (credit_note_id, line_number);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_payment_attempts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            provider_operation_id UUID NULL,
            mandate_id UUID NULL,
            provider_release_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            external_payment_ref VARCHAR(200) NULL,
            idempotency_key VARCHAR(160) NOT NULL,
            attempt_number INTEGER NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency_code CHAR(3) NOT NULL,
            status TEXT NOT NULL,
            failure_classification VARCHAR(80) NULL,
            provider_evidence_sha256 CHAR(64) NULL,
            provider_evidence_ref VARCHAR(300) NULL,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_payment_attempts_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_payment_attempts_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_payment_attempts_operation_org
                FOREIGN KEY (provider_operation_id, organization_id)
                REFERENCES public.platform_provider_operations(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_payment_attempts_mandate_org
                FOREIGN KEY (mandate_id, organization_id)
                REFERENCES public.platform_mandates(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_payment_attempts_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_payment_attempts_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_payment_attempts_idempotency
                UNIQUE (organization_id, idempotency_key),
            CONSTRAINT uq_platform_payment_attempts_invoice_attempt
                UNIQUE (invoice_id, attempt_number),
            CONSTRAINT uq_platform_payment_attempts_provider_ref
                UNIQUE (provider_code, external_payment_ref),
            CONSTRAINT chk_platform_payment_attempts_provider_code
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_payment_attempts_idempotency
                CHECK (btrim(idempotency_key) <> ''),
            CONSTRAINT chk_platform_payment_attempts_attempt_number CHECK (attempt_number > 0),
            CONSTRAINT chk_platform_payment_attempts_amount CHECK (amount_minor > 0),
            CONSTRAINT chk_platform_payment_attempts_currency
                CHECK (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_payment_attempts_status
                CHECK (status IN ('requires_action', 'processing', 'succeeded', 'failed', 'unknown', 'canceled')),
            CONSTRAINT chk_platform_payment_attempts_evidence_sha
                CHECK (
                    provider_evidence_sha256 IS NULL
                    OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_payment_attempts_terminal_completed
                CHECK (
                    status NOT IN ('succeeded', 'failed', 'canceled')
                    OR completed_at IS NOT NULL
                ),
            CONSTRAINT chk_platform_payment_attempts_version CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_payment_attempts_org_invoice "
        "ON public.platform_payment_attempts (organization_id, invoice_id);"
    )
    op.execute(
        "CREATE INDEX ix_platform_payment_attempts_org_status "
        "ON public.platform_payment_attempts (organization_id, status);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_refunds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            payment_attempt_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            credit_note_id UUID NULL,
            provider_operation_id UUID NULL,
            provider_release_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            provider_refund_ref VARCHAR(200) NULL,
            idempotency_key VARCHAR(160) NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency_code CHAR(3) NOT NULL,
            reason_code VARCHAR(80) NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            provider_evidence_sha256 CHAR(64) NULL,
            provider_evidence_ref VARCHAR(300) NULL,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_refunds_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_refunds_payment_attempt_org
                FOREIGN KEY (payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_refunds_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_refunds_credit_note_org
                FOREIGN KEY (credit_note_id, organization_id)
                REFERENCES public.platform_credit_notes(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_refunds_operation_org
                FOREIGN KEY (provider_operation_id, organization_id)
                REFERENCES public.platform_provider_operations(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_refunds_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_refunds_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_refunds_idempotency UNIQUE (organization_id, idempotency_key),
            CONSTRAINT uq_platform_refunds_provider_ref UNIQUE (provider_code, provider_refund_ref),
            CONSTRAINT chk_platform_refunds_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_refunds_idempotency CHECK (btrim(idempotency_key) <> ''),
            CONSTRAINT chk_platform_refunds_amount CHECK (amount_minor > 0),
            CONSTRAINT chk_platform_refunds_currency
                CHECK (currency_code = upper(currency_code) AND currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_platform_refunds_reason CHECK (btrim(reason_code) <> ''),
            CONSTRAINT chk_platform_refunds_status
                CHECK (
                    status IN (
                        'requested', 'approved', 'provider_pending',
                        'succeeded', 'failed', 'unknown', 'canceled'
                    )
                ),
            CONSTRAINT chk_platform_refunds_evidence_sha
                CHECK (
                    provider_evidence_sha256 IS NULL
                    OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_refunds_terminal_completed
                CHECK (
                    status NOT IN ('succeeded', 'failed', 'canceled')
                    OR completed_at IS NOT NULL
                ),
            CONSTRAINT chk_platform_refunds_version CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_refunds_org_payment "
        "ON public.platform_refunds (organization_id, payment_attempt_id);"
    )
    op.execute(
        "CREATE INDEX ix_platform_refunds_org_status "
        "ON public.platform_refunds (organization_id, status);"
    )

    # Reuse the Phase-1 updated_at helper; PAY-11 does not replace its ownership.
    for table_name in (
        "platform_provider_subscriptions",
        "platform_mandates",
        "platform_document_sequences",
        "platform_invoices",
        "platform_credit_notes",
        "platform_payment_attempts",
        "platform_refunds",
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
        CREATE FUNCTION public.pay11_validate_catalog_release_item()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            release_status TEXT;
            price_plan UUID;
        BEGIN
            SELECT status INTO release_status
            FROM public.platform_catalog_releases
            WHERE id = NEW.catalog_release_id;

            IF release_status IS NULL THEN
                RAISE EXCEPTION 'PAY-11 catalog release does not exist';
            END IF;
            IF release_status <> 'draft' THEN
                RAISE EXCEPTION 'PAY-11 published catalog release membership is immutable';
            END IF;

            IF NEW.price_id IS NOT NULL THEN
                SELECT plan_version_id INTO price_plan
                FROM public.platform_prices
                WHERE id = NEW.price_id;
                IF price_plan IS DISTINCT FROM NEW.plan_version_id THEN
                    RAISE EXCEPTION 'PAY-11 catalog release price must belong to its plan version';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_catalog_release_items_validate
        BEFORE INSERT OR UPDATE ON public.platform_catalog_release_items
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_catalog_release_item();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_protect_catalog_release()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            invalid_items BIGINT;
            item_count BIGINT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('published', 'retired') THEN
                    RAISE EXCEPTION 'PAY-11 published catalog releases are immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('published', 'retired') THEN
                IF OLD.status = 'published'
                   AND NEW.status = 'retired'
                   AND NEW.retired_at IS NOT NULL
                   AND ROW(
                       NEW.id, NEW.code, NEW.version, NEW.manifest_json,
                       NEW.manifest_sha256, NEW.published_at, NEW.created_by, NEW.created_at
                   ) IS NOT DISTINCT FROM ROW(
                       OLD.id, OLD.code, OLD.version, OLD.manifest_json,
                       OLD.manifest_sha256, OLD.published_at, OLD.created_by, OLD.created_at
                   ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'PAY-11 published catalog release is immutable except retirement metadata';
            END IF;

            IF NEW.status = 'published' THEN
                SELECT count(*) INTO item_count
                FROM public.platform_catalog_release_items
                WHERE catalog_release_id = NEW.id;
                IF item_count = 0 THEN
                    RAISE EXCEPTION 'PAY-11 catalog release cannot publish without release items';
                END IF;

                SELECT count(*) INTO invalid_items
                FROM public.platform_catalog_release_items cri
                JOIN public.platform_plan_versions pv ON pv.id = cri.plan_version_id
                LEFT JOIN public.platform_prices pp ON pp.id = cri.price_id
                WHERE cri.catalog_release_id = NEW.id
                  AND (
                      pv.status <> 'published'
                      OR (cri.price_id IS NOT NULL AND pp.status <> 'active')
                  );
                IF invalid_items <> 0 THEN
                    RAISE EXCEPTION 'PAY-11 catalog release references unpublished plan or inactive price';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_catalog_releases_immutable
        BEFORE UPDATE OR DELETE ON public.platform_catalog_releases
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_protect_catalog_release();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_protect_catalog_release_item_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_status TEXT;
        BEGIN
            SELECT status INTO parent_status
            FROM public.platform_catalog_releases
            WHERE id = OLD.catalog_release_id;
            IF parent_status IN ('published', 'retired') THEN
                RAISE EXCEPTION 'PAY-11 published catalog release items are immutable';
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
        CREATE TRIGGER trg_platform_catalog_release_items_immutable
        BEFORE UPDATE OR DELETE ON public.platform_catalog_release_items
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_protect_catalog_release_item_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_protect_provider_release()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('published', 'retired') THEN
                    RAISE EXCEPTION 'PAY-11 published provider releases are immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('published', 'retired') THEN
                IF OLD.status = 'published'
                   AND NEW.status = 'retired'
                   AND NEW.retired_at IS NOT NULL
                   AND ROW(
                       NEW.id, NEW.code, NEW.version, NEW.provider_code,
                       NEW.environment, NEW.adapter_version, NEW.contract_sha256,
                       NEW.manifest_json, NEW.manifest_sha256, NEW.published_at,
                       NEW.created_by, NEW.created_at
                   ) IS NOT DISTINCT FROM ROW(
                       OLD.id, OLD.code, OLD.version, OLD.provider_code,
                       OLD.environment, OLD.adapter_version, OLD.contract_sha256,
                       OLD.manifest_json, OLD.manifest_sha256, OLD.published_at,
                       OLD.created_by, OLD.created_at
                   ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'PAY-11 published provider release is immutable except retirement metadata';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_provider_releases_immutable
        BEFORE UPDATE OR DELETE ON public.platform_provider_releases
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_protect_provider_release();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_subscription_contract()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            catalog_status TEXT;
            provider_status TEXT;
            binding_exists BOOLEAN;
            contract_changed BOOLEAN;
        BEGIN
            IF NEW.accepted_catalog_release_id IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT status INTO catalog_status
            FROM public.platform_catalog_releases
            WHERE id = NEW.accepted_catalog_release_id;
            IF catalog_status NOT IN ('published', 'retired') THEN
                RAISE EXCEPTION 'PAY-11 subscription contract requires a published catalog release';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM public.platform_catalog_release_items cri
                WHERE cri.catalog_release_id = NEW.accepted_catalog_release_id
                  AND cri.plan_version_id = NEW.accepted_plan_version_id
                  AND cri.price_id IS NOT DISTINCT FROM NEW.accepted_price_id
            ) INTO binding_exists;
            IF NOT binding_exists THEN
                RAISE EXCEPTION 'PAY-11 subscription plan/price is not in accepted catalog release';
            END IF;

            IF NEW.current_plan_version_id IS DISTINCT FROM NEW.accepted_plan_version_id
               OR NEW.current_price_id IS DISTINCT FROM NEW.accepted_price_id THEN
                RAISE EXCEPTION 'PAY-11 current subscription contract must match accepted commercial binding';
            END IF;

            IF NEW.accepted_provider_release_id IS NOT NULL THEN
                SELECT status INTO provider_status
                FROM public.platform_provider_releases
                WHERE id = NEW.accepted_provider_release_id;
                IF provider_status NOT IN ('published', 'retired') THEN
                    RAISE EXCEPTION 'PAY-11 subscription contract requires a published provider release';
                END IF;
            END IF;

            IF TG_OP = 'UPDATE' AND OLD.accepted_catalog_release_id IS NOT NULL THEN
                contract_changed :=
                    OLD.accepted_catalog_release_id IS DISTINCT FROM NEW.accepted_catalog_release_id
                    OR OLD.accepted_provider_release_id IS DISTINCT FROM NEW.accepted_provider_release_id
                    OR OLD.accepted_plan_version_id IS DISTINCT FROM NEW.accepted_plan_version_id
                    OR OLD.accepted_price_id IS DISTINCT FROM NEW.accepted_price_id
                    OR OLD.commercial_contract_sha256 IS DISTINCT FROM NEW.commercial_contract_sha256
                    OR OLD.commercial_contract_accepted_at IS DISTINCT FROM NEW.commercial_contract_accepted_at;
                IF contract_changed
                   AND (
                       current_setting('app.platform_contract_migration', true) IS DISTINCT FROM 'on'
                       OR current_user <> 'migration_owner'
                   ) THEN
                    RAISE EXCEPTION 'PAY-11 accepted commercial contract cannot drift outside controlled migration';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_subscriptions_commercial_contract
        BEFORE INSERT OR UPDATE OF
            current_plan_version_id,
            current_price_id,
            accepted_catalog_release_id,
            accepted_provider_release_id,
            accepted_plan_version_id,
            accepted_price_id,
            commercial_contract_sha256,
            commercial_contract_accepted_at,
            commercial_contract_migrated_at,
            commercial_contract_migration_reason
        ON public.platform_subscriptions
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_subscription_contract();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.migrate_platform_subscription_commercial_contract(
            p_subscription_id UUID,
            p_organization_id UUID,
            p_catalog_release_id UUID,
            p_provider_release_id UUID,
            p_plan_version_id UUID,
            p_price_id UUID,
            p_contract_sha256 CHAR(64),
            p_reason TEXT,
            p_actor_id UUID DEFAULT NULL
        )
        RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            old_hash CHAR(64);
            catalog_status TEXT;
            provider_status TEXT;
        BEGIN
            IF p_contract_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'PAY-11 controlled contract migration requires canonical sha256';
            END IF;
            IF p_reason IS NULL OR btrim(p_reason) = '' THEN
                RAISE EXCEPTION 'PAY-11 controlled contract migration requires reason';
            END IF;

            SELECT status INTO catalog_status
            FROM public.platform_catalog_releases
            WHERE id = p_catalog_release_id;
            IF catalog_status <> 'published' THEN
                RAISE EXCEPTION 'PAY-11 controlled migration target catalog release must be published';
            END IF;

            IF p_provider_release_id IS NOT NULL THEN
                SELECT status INTO provider_status
                FROM public.platform_provider_releases
                WHERE id = p_provider_release_id;
                IF provider_status <> 'published' THEN
                    RAISE EXCEPTION 'PAY-11 controlled migration target provider release must be published';
                END IF;
            END IF;

            PERFORM pg_catalog.set_config('app.current_org_id', p_organization_id::text, true);
            PERFORM pg_catalog.set_config('app.platform_contract_migration', 'on', true);

            SELECT commercial_contract_sha256
            INTO old_hash
            FROM public.platform_subscriptions
            WHERE id = p_subscription_id
              AND organization_id = p_organization_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'PAY-11 subscription not found for controlled contract migration';
            END IF;

            UPDATE public.platform_subscriptions
            SET
                current_plan_version_id = p_plan_version_id,
                current_price_id = p_price_id,
                accepted_catalog_release_id = p_catalog_release_id,
                accepted_provider_release_id = p_provider_release_id,
                accepted_plan_version_id = p_plan_version_id,
                accepted_price_id = p_price_id,
                commercial_contract_sha256 = p_contract_sha256,
                commercial_contract_accepted_at = clock_timestamp(),
                commercial_contract_migrated_at = clock_timestamp(),
                commercial_contract_migration_reason = p_reason,
                updated_by = p_actor_id,
                version = version + 1
            WHERE id = p_subscription_id
              AND organization_id = p_organization_id;

            INSERT INTO public.platform_billing_audit_events (
                organization_id,
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                before_hash,
                after_hash,
                metadata_redacted_json,
                outcome,
                reason_code
            )
            VALUES (
                p_organization_id,
                CASE WHEN p_actor_id IS NULL THEN 'system' ELSE 'support' END,
                p_actor_id,
                'platform.subscription.commercial_contract_migrated',
                'platform_subscription',
                p_subscription_id,
                old_hash,
                p_contract_sha256,
                jsonb_build_object(
                    'catalog_release_id', p_catalog_release_id,
                    'provider_release_id', p_provider_release_id,
                    'plan_version_id', p_plan_version_id,
                    'price_id', p_price_id
                ),
                'succeeded',
                'controlled_commercial_migration'
            );
        END;
        $$;
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION public.migrate_platform_subscription_commercial_contract(
            UUID, UUID, UUID, UUID, UUID, UUID, CHAR(64), TEXT, UUID
        ) FROM PUBLIC;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_security_owner') THEN
                GRANT EXECUTE ON FUNCTION public.migrate_platform_subscription_commercial_contract(
                    UUID, UUID, UUID, UUID, UUID, UUID, CHAR(64), TEXT, UUID
                ) TO app_security_owner;
            END IF;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_invoice_line_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_status TEXT;
            target_invoice UUID;
        BEGIN
            target_invoice := CASE WHEN TG_OP = 'DELETE' THEN OLD.invoice_id ELSE NEW.invoice_id END;
            SELECT status INTO parent_status
            FROM public.platform_invoices
            WHERE id = target_invoice
            FOR UPDATE;
            IF parent_status IS NULL THEN
                RAISE EXCEPTION 'PAY-11 invoice line parent does not exist';
            END IF;
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION 'PAY-11 issued invoice lines are immutable';
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
        CREATE TRIGGER trg_platform_invoice_lines_immutable_after_issue
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_invoice_lines
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_invoice_line_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_invoice_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            line_count BIGINT;
            line_net BIGINT;
            line_tax BIGINT;
            line_gross BIGINT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'PAY-11 issued invoices cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'PAY-11 invoices must be created as draft before issuance';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status IN ('issued', 'partially_paid', 'paid', 'void') THEN
                IF ROW(
                    NEW.organization_id, NEW.billing_account_id, NEW.subscription_id,
                    NEW.document_sequence_id, NEW.invoice_number, NEW.currency_code,
                    NEW.subtotal_minor, NEW.tax_minor, NEW.total_minor,
                    NEW.catalog_release_id, NEW.provider_release_id,
                    NEW.plan_version_id, NEW.price_id, NEW.commercial_contract_sha256,
                    NEW.tax_snapshot_json, NEW.billing_address_snapshot_json,
                    NEW.provider_code, NEW.provider_invoice_ref,
                    NEW.artifact_sha256, NEW.artifact_ref, NEW.issued_at, NEW.due_at,
                    NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.organization_id, OLD.billing_account_id, OLD.subscription_id,
                    OLD.document_sequence_id, OLD.invoice_number, OLD.currency_code,
                    OLD.subtotal_minor, OLD.tax_minor, OLD.total_minor,
                    OLD.catalog_release_id, OLD.provider_release_id,
                    OLD.plan_version_id, OLD.price_id, OLD.commercial_contract_sha256,
                    OLD.tax_snapshot_json, OLD.billing_address_snapshot_json,
                    OLD.provider_code, OLD.provider_invoice_ref,
                    OLD.artifact_sha256, OLD.artifact_ref, OLD.issued_at, OLD.due_at,
                    OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'PAY-11 issued invoice legal fields are immutable';
                END IF;
                IF NEW.amount_due_minor > OLD.amount_due_minor THEN
                    RAISE EXCEPTION 'PAY-11 invoice amount due cannot increase after issuance';
                END IF;
                IF NOT (
                    NEW.status = OLD.status
                    OR (OLD.status = 'issued' AND NEW.status IN ('partially_paid', 'paid', 'void'))
                    OR (OLD.status = 'partially_paid' AND NEW.status = 'paid')
                ) THEN
                    RAISE EXCEPTION 'PAY-11 forbidden invoice status transition: % -> %', OLD.status, NEW.status;
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'draft' AND NEW.status = 'issued' THEN
                SELECT count(*), COALESCE(sum(net_amount_minor), 0),
                       COALESCE(sum(tax_amount_minor), 0),
                       COALESCE(sum(gross_amount_minor), 0)
                INTO line_count, line_net, line_tax, line_gross
                FROM public.platform_invoice_lines
                WHERE invoice_id = NEW.id;
                IF line_count = 0 THEN
                    RAISE EXCEPTION 'PAY-11 invoice cannot issue without lines';
                END IF;
                IF line_net <> NEW.subtotal_minor
                   OR line_tax <> NEW.tax_minor
                   OR line_gross <> NEW.total_minor THEN
                    RAISE EXCEPTION 'PAY-11 invoice header totals must equal immutable line snapshot totals';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'draft' AND NEW.status <> 'draft' THEN
                RAISE EXCEPTION 'PAY-11 draft invoice may only transition to issued';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_invoices_legal_immutability
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_invoices
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_invoice_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_credit_note_line_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_status TEXT;
            target_note UUID;
        BEGIN
            target_note := CASE WHEN TG_OP = 'DELETE' THEN OLD.credit_note_id ELSE NEW.credit_note_id END;
            SELECT status INTO parent_status
            FROM public.platform_credit_notes
            WHERE id = target_note
            FOR UPDATE;
            IF parent_status IS NULL THEN
                RAISE EXCEPTION 'PAY-11 credit-note line parent does not exist';
            END IF;
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION 'PAY-11 issued credit-note lines are immutable';
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
        CREATE TRIGGER trg_platform_credit_note_lines_immutable_after_issue
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_credit_note_lines
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_credit_note_line_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_credit_note_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            invoice_currency CHAR(3);
            invoice_total BIGINT;
            line_count BIGINT;
            line_net BIGINT;
            line_tax BIGINT;
            line_gross BIGINT;
            other_issued BIGINT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'PAY-11 issued credit notes cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'PAY-11 credit notes must be created as draft before issuance';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status IN ('issued', 'applied', 'void') THEN
                IF ROW(
                    NEW.organization_id, NEW.invoice_id, NEW.document_sequence_id,
                    NEW.credit_note_number, NEW.reason_code, NEW.currency_code,
                    NEW.subtotal_minor, NEW.tax_minor, NEW.total_minor,
                    NEW.artifact_sha256, NEW.artifact_ref, NEW.issued_at, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.organization_id, OLD.invoice_id, OLD.document_sequence_id,
                    OLD.credit_note_number, OLD.reason_code, OLD.currency_code,
                    OLD.subtotal_minor, OLD.tax_minor, OLD.total_minor,
                    OLD.artifact_sha256, OLD.artifact_ref, OLD.issued_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'PAY-11 issued credit-note legal fields are immutable';
                END IF;
                IF NOT (
                    NEW.status = OLD.status
                    OR (OLD.status = 'issued' AND NEW.status IN ('applied', 'void'))
                ) THEN
                    RAISE EXCEPTION 'PAY-11 forbidden credit-note status transition: % -> %', OLD.status, NEW.status;
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'draft' AND NEW.status = 'issued' THEN
                SELECT currency_code, total_minor
                INTO invoice_currency, invoice_total
                FROM public.platform_invoices
                WHERE id = NEW.invoice_id
                FOR UPDATE;
                IF invoice_currency IS NULL THEN
                    RAISE EXCEPTION 'PAY-11 credit note invoice does not exist';
                END IF;
                IF NEW.currency_code <> invoice_currency THEN
                    RAISE EXCEPTION 'PAY-11 credit note currency must match invoice currency';
                END IF;

                SELECT count(*), COALESCE(sum(net_amount_minor), 0),
                       COALESCE(sum(tax_amount_minor), 0),
                       COALESCE(sum(gross_amount_minor), 0)
                INTO line_count, line_net, line_tax, line_gross
                FROM public.platform_credit_note_lines
                WHERE credit_note_id = NEW.id;
                IF line_count = 0 THEN
                    RAISE EXCEPTION 'PAY-11 credit note cannot issue without lines';
                END IF;
                IF line_net <> NEW.subtotal_minor
                   OR line_tax <> NEW.tax_minor
                   OR line_gross <> NEW.total_minor THEN
                    RAISE EXCEPTION 'PAY-11 credit-note header totals must equal immutable line totals';
                END IF;

                SELECT COALESCE(sum(total_minor), 0)
                INTO other_issued
                FROM public.platform_credit_notes
                WHERE invoice_id = NEW.invoice_id
                  AND id <> NEW.id
                  AND status IN ('issued', 'applied');
                IF other_issued + NEW.total_minor > invoice_total THEN
                    RAISE EXCEPTION 'PAY-11 cumulative credit notes cannot exceed invoice total';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'draft' AND NEW.status <> 'draft' THEN
                RAISE EXCEPTION 'PAY-11 draft credit note may only transition to issued';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_credit_notes_legal_immutability
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_credit_notes
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_credit_note_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_payment_attempt()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            invoice_currency CHAR(3);
            invoice_total BIGINT;
            invoice_status TEXT;
        BEGIN
            SELECT currency_code, total_minor, status
            INTO invoice_currency, invoice_total, invoice_status
            FROM public.platform_invoices
            WHERE id = NEW.invoice_id
            FOR UPDATE;

            IF invoice_currency IS NULL THEN
                RAISE EXCEPTION 'PAY-11 payment attempt invoice does not exist';
            END IF;
            IF invoice_status NOT IN ('issued', 'partially_paid') THEN
                RAISE EXCEPTION 'PAY-11 payment attempts require an issued unpaid invoice';
            END IF;
            IF NEW.currency_code <> invoice_currency THEN
                RAISE EXCEPTION 'PAY-11 payment attempt currency must match invoice currency';
            END IF;
            IF NEW.amount_minor > invoice_total THEN
                RAISE EXCEPTION 'PAY-11 payment attempt cannot exceed invoice total';
            END IF;

            IF TG_OP = 'UPDATE' THEN
                IF ROW(
                    NEW.organization_id, NEW.invoice_id, NEW.provider_operation_id,
                    NEW.mandate_id, NEW.provider_release_id, NEW.provider_code,
                    NEW.idempotency_key, NEW.attempt_number, NEW.amount_minor,
                    NEW.currency_code, NEW.attempted_at, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.organization_id, OLD.invoice_id, OLD.provider_operation_id,
                    OLD.mandate_id, OLD.provider_release_id, OLD.provider_code,
                    OLD.idempotency_key, OLD.attempt_number, OLD.amount_minor,
                    OLD.currency_code, OLD.attempted_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'PAY-11 payment attempt financial identity is immutable';
                END IF;

                IF OLD.external_payment_ref IS NOT NULL
                   AND NEW.external_payment_ref IS DISTINCT FROM OLD.external_payment_ref THEN
                    RAISE EXCEPTION 'PAY-11 provider payment reference cannot be rewritten';
                END IF;
                IF OLD.provider_evidence_sha256 IS NOT NULL
                   AND NEW.provider_evidence_sha256 IS DISTINCT FROM OLD.provider_evidence_sha256 THEN
                    RAISE EXCEPTION 'PAY-11 payment evidence hash cannot be rewritten';
                END IF;
                IF OLD.provider_evidence_ref IS NOT NULL
                   AND NEW.provider_evidence_ref IS DISTINCT FROM OLD.provider_evidence_ref THEN
                    RAISE EXCEPTION 'PAY-11 payment evidence reference cannot be rewritten';
                END IF;
                IF OLD.completed_at IS NOT NULL
                   AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                    RAISE EXCEPTION 'PAY-11 payment completion timestamp cannot be rewritten';
                END IF;

                IF OLD.status IN ('succeeded', 'failed', 'canceled')
                   AND NEW.status IS DISTINCT FROM OLD.status THEN
                    RAISE EXCEPTION 'PAY-11 terminal payment fact cannot revert';
                END IF;

                IF NOT (
                    NEW.status = OLD.status
                    OR (
                        OLD.status = 'requires_action'
                        AND NEW.status IN ('processing', 'succeeded', 'failed', 'unknown', 'canceled')
                    )
                    OR (
                        OLD.status = 'processing'
                        AND NEW.status IN ('succeeded', 'failed', 'unknown', 'canceled')
                    )
                    OR (
                        OLD.status = 'unknown'
                        AND NEW.status IN ('processing', 'succeeded', 'failed', 'canceled')
                    )
                ) THEN
                    RAISE EXCEPTION 'PAY-11 forbidden payment status transition: % -> %', OLD.status, NEW.status;
                END IF;
            END IF;

            IF NEW.status = 'succeeded' THEN
                IF NEW.external_payment_ref IS NULL
                   OR btrim(NEW.external_payment_ref) = ''
                   OR NEW.provider_evidence_sha256 IS NULL
                   OR NEW.provider_evidence_ref IS NULL
                   OR btrim(NEW.provider_evidence_ref) = ''
                   OR NEW.completed_at IS NULL THEN
                    RAISE EXCEPTION 'PAY-11 succeeded payment requires provider reference, evidence, and completion timestamp';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_payment_attempts_validate
        BEFORE INSERT OR UPDATE ON public.platform_payment_attempts
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_payment_attempt();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay11_validate_refund()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            payment_amount BIGINT;
            payment_currency CHAR(3);
            payment_status TEXT;
            payment_invoice UUID;
            reserved_refunds BIGINT;
            note_status TEXT;
            note_invoice UUID;
        BEGIN
            SELECT amount_minor, currency_code, status, invoice_id
            INTO payment_amount, payment_currency, payment_status, payment_invoice
            FROM public.platform_payment_attempts
            WHERE id = NEW.payment_attempt_id
            FOR UPDATE;

            IF payment_status <> 'succeeded' THEN
                RAISE EXCEPTION 'PAY-11 refunds require a succeeded payment attempt';
            END IF;
            IF NEW.invoice_id <> payment_invoice THEN
                RAISE EXCEPTION 'PAY-11 refund invoice must match payment attempt invoice';
            END IF;
            IF NEW.currency_code <> payment_currency THEN
                RAISE EXCEPTION 'PAY-11 refund currency must match payment attempt currency';
            END IF;

            IF TG_OP = 'UPDATE' THEN
                IF ROW(
                    NEW.organization_id, NEW.payment_attempt_id, NEW.invoice_id,
                    NEW.provider_operation_id, NEW.provider_release_id,
                    NEW.provider_code, NEW.idempotency_key, NEW.amount_minor,
                    NEW.currency_code, NEW.reason_code, NEW.requested_at, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.organization_id, OLD.payment_attempt_id, OLD.invoice_id,
                    OLD.provider_operation_id, OLD.provider_release_id,
                    OLD.provider_code, OLD.idempotency_key, OLD.amount_minor,
                    OLD.currency_code, OLD.reason_code, OLD.requested_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'PAY-11 refund financial identity is immutable';
                END IF;

                IF OLD.credit_note_id IS NOT NULL
                   AND NEW.credit_note_id IS DISTINCT FROM OLD.credit_note_id THEN
                    RAISE EXCEPTION 'PAY-11 refund credit-note binding cannot be rewritten';
                END IF;
                IF OLD.provider_refund_ref IS NOT NULL
                   AND NEW.provider_refund_ref IS DISTINCT FROM OLD.provider_refund_ref THEN
                    RAISE EXCEPTION 'PAY-11 provider refund reference cannot be rewritten';
                END IF;
                IF OLD.provider_evidence_sha256 IS NOT NULL
                   AND NEW.provider_evidence_sha256 IS DISTINCT FROM OLD.provider_evidence_sha256 THEN
                    RAISE EXCEPTION 'PAY-11 refund evidence hash cannot be rewritten';
                END IF;
                IF OLD.provider_evidence_ref IS NOT NULL
                   AND NEW.provider_evidence_ref IS DISTINCT FROM OLD.provider_evidence_ref THEN
                    RAISE EXCEPTION 'PAY-11 refund evidence reference cannot be rewritten';
                END IF;
                IF OLD.completed_at IS NOT NULL
                   AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                    RAISE EXCEPTION 'PAY-11 refund completion timestamp cannot be rewritten';
                END IF;

                IF OLD.status IN ('succeeded', 'failed', 'canceled')
                   AND NEW.status IS DISTINCT FROM OLD.status THEN
                    RAISE EXCEPTION 'PAY-11 terminal refund fact cannot revert';
                END IF;

                IF NOT (
                    NEW.status = OLD.status
                    OR (
                        OLD.status = 'requested'
                        AND NEW.status IN ('approved', 'provider_pending', 'failed', 'canceled')
                    )
                    OR (
                        OLD.status = 'approved'
                        AND NEW.status IN ('provider_pending', 'failed', 'canceled')
                    )
                    OR (
                        OLD.status = 'provider_pending'
                        AND NEW.status IN ('succeeded', 'failed', 'unknown', 'canceled')
                    )
                    OR (
                        OLD.status = 'unknown'
                        AND NEW.status IN ('provider_pending', 'succeeded', 'failed', 'canceled')
                    )
                ) THEN
                    RAISE EXCEPTION 'PAY-11 forbidden refund status transition: % -> %', OLD.status, NEW.status;
                END IF;
            END IF;

            IF NEW.status NOT IN ('failed', 'canceled') THEN
                SELECT COALESCE(sum(amount_minor), 0)
                INTO reserved_refunds
                FROM public.platform_refunds
                WHERE payment_attempt_id = NEW.payment_attempt_id
                  AND id <> NEW.id
                  AND status NOT IN ('failed', 'canceled');
                IF reserved_refunds + NEW.amount_minor > payment_amount THEN
                    RAISE EXCEPTION 'PAY-11 cumulative refunds cannot exceed captured payment';
                END IF;
            END IF;

            IF NEW.status = 'succeeded' THEN
                IF NEW.credit_note_id IS NULL
                   OR NEW.provider_refund_ref IS NULL
                   OR btrim(NEW.provider_refund_ref) = ''
                   OR NEW.provider_evidence_sha256 IS NULL
                   OR NEW.provider_evidence_ref IS NULL
                   OR btrim(NEW.provider_evidence_ref) = ''
                   OR NEW.completed_at IS NULL THEN
                    RAISE EXCEPTION 'PAY-11 succeeded refund requires credit note and authoritative provider evidence';
                END IF;
                SELECT status, invoice_id INTO note_status, note_invoice
                FROM public.platform_credit_notes
                WHERE id = NEW.credit_note_id;
                IF note_status NOT IN ('issued', 'applied') OR note_invoice <> NEW.invoice_id THEN
                    RAISE EXCEPTION 'PAY-11 succeeded refund requires issued credit note for the same invoice';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_refunds_validate
        BEFORE INSERT OR UPDATE ON public.platform_refunds
        FOR EACH ROW
        EXECUTE FUNCTION public.pay11_validate_refund();
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
                    public.platform_catalog_releases,
                    public.platform_catalog_release_items,
                    public.platform_provider_releases,
                    public.platform_provider_subscriptions,
                    public.platform_mandates,
                    public.platform_invoices,
                    public.platform_invoice_lines,
                    public.platform_payment_attempts,
                    public.platform_refunds,
                    public.platform_credit_notes,
                    public.platform_credit_note_lines
                TO app_runtime;
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    # PAY-11 tenant relations use FORCE RLS. Temporarily remove FORCE only
    # inside this transactional downgrade so the table owner can see every
    # row before destructive DDL. A blocked downgrade rolls this DDL back.
    for table_name in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY;"
        )

    op.execute(
        """
        DO $pay11_downgrade_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.platform_provider_subscriptions)
               OR EXISTS (SELECT 1 FROM public.platform_mandates)
               OR EXISTS (SELECT 1 FROM public.platform_invoices)
               OR EXISTS (SELECT 1 FROM public.platform_invoice_lines)
               OR EXISTS (SELECT 1 FROM public.platform_payment_attempts)
               OR EXISTS (SELECT 1 FROM public.platform_refunds)
               OR EXISTS (SELECT 1 FROM public.platform_credit_notes)
               OR EXISTS (SELECT 1 FROM public.platform_credit_note_lines)
               OR EXISTS (SELECT 1 FROM public.platform_document_sequences)
               OR EXISTS (SELECT 1 FROM public.platform_catalog_release_items)
               OR EXISTS (SELECT 1 FROM public.platform_catalog_releases)
               OR EXISTS (SELECT 1 FROM public.platform_provider_releases)
               OR EXISTS (
                    SELECT 1
                    FROM public.platform_subscriptions
                    WHERE accepted_catalog_release_id IS NOT NULL
                       OR accepted_provider_release_id IS NOT NULL
                       OR accepted_plan_version_id IS NOT NULL
                       OR accepted_price_id IS NOT NULL
                       OR commercial_contract_sha256 IS NOT NULL
                       OR commercial_contract_accepted_at IS NOT NULL
                       OR commercial_contract_migrated_at IS NOT NULL
                       OR commercial_contract_migration_reason IS NOT NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-11 downgrade blocked: production billing records or accepted commercial bindings exist';
            END IF;
        END
        $pay11_downgrade_guard$;
        """
    )

    for table_name in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table_name} ON public.{table_name};")

    op.execute("DROP TRIGGER IF EXISTS trg_platform_refunds_validate ON public.platform_refunds;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_refund();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_payment_attempts_validate ON public.platform_payment_attempts;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_payment_attempt();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_credit_notes_legal_immutability ON public.platform_credit_notes;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_credit_note_transition();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_credit_note_lines_immutable_after_issue ON public.platform_credit_note_lines;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_credit_note_line_mutation();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_invoices_legal_immutability ON public.platform_invoices;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_invoice_transition();")
    op.execute("DROP TRIGGER IF EXISTS trg_platform_invoice_lines_immutable_after_issue ON public.platform_invoice_lines;")
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_invoice_line_mutation();")

    op.execute(
        """
        DROP FUNCTION IF EXISTS public.migrate_platform_subscription_commercial_contract(
            UUID, UUID, UUID, UUID, UUID, UUID, CHAR(64), TEXT, UUID
        );
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_subscriptions_commercial_contract "
        "ON public.platform_subscriptions;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_subscription_contract();")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_provider_releases_immutable "
        "ON public.platform_provider_releases;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.pay11_protect_provider_release();")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_catalog_release_items_immutable "
        "ON public.platform_catalog_release_items;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.pay11_protect_catalog_release_item_mutation();")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_catalog_releases_immutable "
        "ON public.platform_catalog_releases;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.pay11_protect_catalog_release();")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_platform_catalog_release_items_validate "
        "ON public.platform_catalog_release_items;"
    )
    op.execute("DROP FUNCTION IF EXISTS public.pay11_validate_catalog_release_item();")

    for table_name in (
        "platform_refunds",
        "platform_payment_attempts",
        "platform_credit_notes",
        "platform_invoices",
        "platform_document_sequences",
        "platform_mandates",
        "platform_provider_subscriptions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON public.{table_name};")

    op.execute("DROP TABLE IF EXISTS public.platform_refunds;")
    op.execute("DROP TABLE IF EXISTS public.platform_payment_attempts;")
    op.execute("DROP TABLE IF EXISTS public.platform_credit_note_lines;")
    op.execute("DROP TABLE IF EXISTS public.platform_credit_notes;")
    op.execute("DROP TABLE IF EXISTS public.platform_invoice_lines;")
    op.execute("DROP TABLE IF EXISTS public.platform_invoices;")
    op.execute("DROP TABLE IF EXISTS public.platform_document_sequences;")
    op.execute("DROP TABLE IF EXISTS public.platform_mandates;")
    op.execute("DROP TABLE IF EXISTS public.platform_provider_subscriptions;")

    op.execute(
        """
        ALTER TABLE public.platform_subscriptions
            DROP CONSTRAINT IF EXISTS chk_platform_subscriptions_contract_migration_metadata,
            DROP CONSTRAINT IF EXISTS chk_platform_subscriptions_commercial_contract_shape,
            DROP CONSTRAINT IF EXISTS chk_platform_subscriptions_commercial_contract_sha,
            DROP CONSTRAINT IF EXISTS fk_platform_subscriptions_accepted_price,
            DROP CONSTRAINT IF EXISTS fk_platform_subscriptions_accepted_plan,
            DROP CONSTRAINT IF EXISTS fk_platform_subscriptions_accepted_provider_release,
            DROP CONSTRAINT IF EXISTS fk_platform_subscriptions_accepted_catalog_release;
        """
    )
    op.execute(
        """
        ALTER TABLE public.platform_subscriptions
            DROP COLUMN IF EXISTS commercial_contract_migration_reason,
            DROP COLUMN IF EXISTS commercial_contract_migrated_at,
            DROP COLUMN IF EXISTS commercial_contract_accepted_at,
            DROP COLUMN IF EXISTS commercial_contract_sha256,
            DROP COLUMN IF EXISTS accepted_price_id,
            DROP COLUMN IF EXISTS accepted_plan_version_id,
            DROP COLUMN IF EXISTS accepted_provider_release_id,
            DROP COLUMN IF EXISTS accepted_catalog_release_id;
        """
    )

    op.execute("DROP TABLE IF EXISTS public.platform_catalog_release_items;")
    op.execute("DROP TABLE IF EXISTS public.platform_provider_releases;")
    op.execute("DROP TABLE IF EXISTS public.platform_catalog_releases;")
