"""platform billing phase 4a provider persistence foundation

Revision ID: 014167728f4a
Revises: f2b3c4d5e6a7
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "014167728f4a"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE_4A_TABLES = (
    "platform_provider_customers",
    "platform_payment_methods",
    "platform_provider_operations",
    "platform_webhook_inbox",
    "platform_reconciliation_runs",
    "platform_reconciliation_items",
)

TENANT_TABLES = (
    "platform_provider_customers",
    "platform_payment_methods",
    "platform_provider_operations",
    "platform_reconciliation_items",
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
            organization_id IS NOT NULL
            AND organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        WITH CHECK (
            organization_id IS NOT NULL
            AND organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        );
        """
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.platform_provider_customers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            external_customer_ref VARCHAR(200) NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_provider_customers_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_provider_customers_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_provider_customers_org_provider UNIQUE (organization_id, provider_code),
            CONSTRAINT uq_platform_provider_customers_provider_external UNIQUE (provider_code, external_customer_ref),
            CONSTRAINT chk_platform_provider_customers_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_provider_customers_external_ref_nonempty CHECK (btrim(external_customer_ref) <> ''),
            CONSTRAINT chk_platform_provider_customers_status CHECK (status IN ('active', 'inactive', 'deleted')),
            CONSTRAINT chk_platform_provider_customers_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_provider_customers_org_status "
        "ON public.platform_provider_customers (organization_id, status);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_payment_methods (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            provider_customer_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            external_payment_method_ref VARCHAR(200) NOT NULL,
            method_type VARCHAR(40) NOT NULL,
            brand VARCHAR(40) NULL,
            last_four CHAR(4) NULL,
            expiry_month SMALLINT NULL,
            expiry_year SMALLINT NULL,
            display_label VARCHAR(120) NULL,
            status TEXT NOT NULL DEFAULT 'active',
            is_default BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_payment_methods_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_payment_methods_customer_org
                FOREIGN KEY (provider_customer_id, organization_id)
                REFERENCES public.platform_provider_customers(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_payment_methods_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_payment_methods_org_provider_external UNIQUE (
                organization_id, provider_code, external_payment_method_ref
            ),
            CONSTRAINT chk_platform_payment_methods_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_payment_methods_external_ref_nonempty CHECK (btrim(external_payment_method_ref) <> ''),
            CONSTRAINT chk_platform_payment_methods_type_nonempty CHECK (btrim(method_type) <> ''),
            CONSTRAINT chk_platform_payment_methods_last_four CHECK (last_four IS NULL OR last_four ~ '^[0-9]{4}$'),
            CONSTRAINT chk_platform_payment_methods_expiry_month CHECK (expiry_month IS NULL OR expiry_month BETWEEN 1 AND 12),
            CONSTRAINT chk_platform_payment_methods_expiry_year CHECK (expiry_year IS NULL OR expiry_year BETWEEN 2020 AND 2200),
            CONSTRAINT chk_platform_payment_methods_status CHECK (status IN ('active', 'inactive', 'expired', 'detached')),
            CONSTRAINT chk_platform_payment_methods_default_active CHECK (is_default = false OR status = 'active'),
            CONSTRAINT chk_platform_payment_methods_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_payment_methods_org_customer "
        "ON public.platform_payment_methods (organization_id, provider_customer_id);"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_payment_methods_one_default_per_provider
        ON public.platform_payment_methods (organization_id, provider_code)
        WHERE status = 'active' AND is_default = true;
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_provider_operations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            operation_type VARCHAR(80) NOT NULL,
            idempotency_key VARCHAR(160) NOT NULL,
            canonical_request_sha256 CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            external_operation_ref VARCHAR(200) NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ NULL,
            result_evidence_sha256 CHAR(64) NULL,
            result_reference VARCHAR(240) NULL,
            error_classification VARCHAR(80) NULL,
            error_detail_safe TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NULL,
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_provider_operations_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_provider_operations_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_provider_operations_idempotency UNIQUE (organization_id, provider_code, idempotency_key),
            CONSTRAINT chk_platform_provider_operations_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_provider_operations_operation_type_nonempty CHECK (btrim(operation_type) <> ''),
            CONSTRAINT chk_platform_provider_operations_idempotency_nonempty CHECK (btrim(idempotency_key) <> ''),
            CONSTRAINT chk_platform_provider_operations_request_hash CHECK (canonical_request_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_provider_operations_status CHECK (
                status IN ('reserved', 'in_progress', 'succeeded', 'failed', 'unknown')
            ),
            CONSTRAINT chk_platform_provider_operations_attempt_count CHECK (attempt_count >= 0),
            CONSTRAINT chk_platform_provider_operations_result_hash CHECK (
                result_evidence_sha256 IS NULL OR result_evidence_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT chk_platform_provider_operations_terminal_completed CHECK (
                status NOT IN ('succeeded', 'failed', 'unknown') OR completed_at IS NOT NULL
            ),
            CONSTRAINT chk_platform_provider_operations_failure_metadata CHECK (
                status <> 'failed' OR error_classification IS NOT NULL
            ),
            CONSTRAINT chk_platform_provider_operations_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_provider_operations_org_status "
        "ON public.platform_provider_operations (organization_id, status);"
    )
    op.execute(
        "CREATE INDEX ix_platform_provider_operations_external_ref "
        "ON public.platform_provider_operations (provider_code, external_operation_ref) "
        "WHERE external_operation_ref IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX ix_platform_provider_operations_retry "
        "ON public.platform_provider_operations (status, next_retry_at) "
        "WHERE status IN ('reserved', 'in_progress', 'unknown');"
    )

    op.execute(
        """
        CREATE TABLE public.platform_webhook_inbox (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_code VARCHAR(40) NOT NULL,
            provider_event_id VARCHAR(200) NOT NULL,
            payload_sha256 CHAR(64) NOT NULL,
            encrypted_payload_ref TEXT NOT NULL,
            normalized_event_type VARCHAR(120) NOT NULL,
            processing_status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            processed_at TIMESTAMPTZ NULL,
            error_classification VARCHAR(80) NULL,
            error_detail_safe TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT uq_platform_webhook_inbox_provider_event UNIQUE (provider_code, provider_event_id),
            CONSTRAINT uq_platform_webhook_inbox_provider_event_hash UNIQUE (provider_code, provider_event_id, payload_sha256),
            CONSTRAINT chk_platform_webhook_inbox_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_webhook_inbox_event_id_nonempty CHECK (btrim(provider_event_id) <> ''),
            CONSTRAINT chk_platform_webhook_inbox_payload_hash CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_webhook_inbox_payload_ref_nonempty CHECK (btrim(encrypted_payload_ref) <> ''),
            CONSTRAINT chk_platform_webhook_inbox_event_type_nonempty CHECK (btrim(normalized_event_type) <> ''),
            CONSTRAINT chk_platform_webhook_inbox_status CHECK (
                processing_status IN ('pending', 'processing', 'processed', 'failed_retryable', 'failed_final', 'ignored')
            ),
            CONSTRAINT chk_platform_webhook_inbox_attempt_count CHECK (attempt_count >= 0),
            CONSTRAINT chk_platform_webhook_inbox_processed_metadata CHECK (
                processing_status <> 'processed' OR processed_at IS NOT NULL
            ),
            CONSTRAINT chk_platform_webhook_inbox_failure_metadata CHECK (
                processing_status NOT IN ('failed_retryable', 'failed_final') OR error_classification IS NOT NULL
            ),
            CONSTRAINT chk_platform_webhook_inbox_version_positive CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_webhook_inbox_status_retry "
        "ON public.platform_webhook_inbox (processing_status, received_at) "
        "WHERE processing_status IN ('pending', 'failed_retryable');"
    )
    op.execute(
        "CREATE INDEX ix_platform_webhook_inbox_event_type "
        "ON public.platform_webhook_inbox (provider_code, normalized_event_type, received_at);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_reconciliation_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider_code VARCHAR(40) NOT NULL,
            run_identity VARCHAR(160) NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            watermark_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NULL,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            discrepancy_count INTEGER NOT NULL DEFAULT 0,
            resolved_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_platform_reconciliation_runs_identity UNIQUE (provider_code, run_identity),
            CONSTRAINT chk_platform_reconciliation_runs_provider_code CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_reconciliation_runs_identity_nonempty CHECK (btrim(run_identity) <> ''),
            CONSTRAINT chk_platform_reconciliation_runs_status CHECK (status IN ('running', 'succeeded', 'failed', 'canceled')),
            CONSTRAINT chk_platform_reconciliation_runs_scope_object CHECK (jsonb_typeof(scope_json) = 'object'),
            CONSTRAINT chk_platform_reconciliation_runs_watermark_object CHECK (jsonb_typeof(watermark_json) = 'object'),
            CONSTRAINT chk_platform_reconciliation_runs_counts_nonnegative CHECK (
                scanned_count >= 0 AND discrepancy_count >= 0 AND resolved_count >= 0 AND failed_count >= 0
            ),
            CONSTRAINT chk_platform_reconciliation_runs_completed_metadata CHECK (
                status = 'running' OR completed_at IS NOT NULL
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_reconciliation_runs_status "
        "ON public.platform_reconciliation_runs (provider_code, status, started_at);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_reconciliation_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reconciliation_run_id UUID NOT NULL,
            organization_id UUID NULL,
            provider_object_type VARCHAR(80) NOT NULL,
            external_object_ref VARCHAR(200) NOT NULL,
            local_object_type VARCHAR(80) NULL,
            local_object_id UUID NULL,
            discrepancy_classification VARCHAR(80) NOT NULL,
            resolution_status TEXT NOT NULL DEFAULT 'open',
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            resolved_at TIMESTAMPTZ NULL,
            CONSTRAINT fk_platform_reconciliation_items_run
                FOREIGN KEY (reconciliation_run_id) REFERENCES public.platform_reconciliation_runs(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_reconciliation_items_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_reconciliation_items_run_discrepancy UNIQUE (
                reconciliation_run_id, provider_object_type, external_object_ref, discrepancy_classification
            ),
            CONSTRAINT chk_platform_reconciliation_items_provider_object_type CHECK (btrim(provider_object_type) <> ''),
            CONSTRAINT chk_platform_reconciliation_items_external_ref CHECK (btrim(external_object_ref) <> ''),
            CONSTRAINT chk_platform_reconciliation_items_local_shape CHECK (
                (local_object_type IS NULL AND local_object_id IS NULL)
                OR (local_object_type IS NOT NULL AND btrim(local_object_type) <> '' AND local_object_id IS NOT NULL)
            ),
            CONSTRAINT chk_platform_reconciliation_items_discrepancy CHECK (btrim(discrepancy_classification) <> ''),
            CONSTRAINT chk_platform_reconciliation_items_resolution_status CHECK (
                resolution_status IN ('open', 'resolved', 'ignored', 'failed')
            ),
            CONSTRAINT chk_platform_reconciliation_items_evidence_hash CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_reconciliation_items_evidence_ref CHECK (btrim(evidence_ref) <> ''),
            CONSTRAINT chk_platform_reconciliation_items_resolved_metadata CHECK (
                resolution_status NOT IN ('resolved', 'ignored', 'failed') OR resolved_at IS NOT NULL
            )
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_reconciliation_items_org_status "
        "ON public.platform_reconciliation_items (organization_id, resolution_status) "
        "WHERE organization_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX ix_platform_reconciliation_items_external "
        "ON public.platform_reconciliation_items (provider_object_type, external_object_ref);"
    )

    for table_name in (
        "platform_provider_customers",
        "platform_payment_methods",
        "platform_provider_operations",
        "platform_webhook_inbox",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_touch_updated_at
            BEFORE UPDATE ON public.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.platform_billing_touch_updated_at();
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
                    public.platform_provider_customers,
                    public.platform_payment_methods,
                    public.platform_provider_operations,
                    public.platform_reconciliation_items
                TO app_runtime;
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    for table_name in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table_name} ON public.{table_name};")

    for table_name in (
        "platform_provider_customers",
        "platform_payment_methods",
        "platform_provider_operations",
        "platform_webhook_inbox",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at ON public.{table_name};")

    for table_name in reversed(PHASE_4A_TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{table_name};")
