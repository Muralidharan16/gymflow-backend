"""PAY-15 legacy payment retirement and data migration

Revision ID: zz17d8e9f0a61
Revises: zz07d8e9f0a60
Create Date: 2026-09-21 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "zz17d8e9f0a61"
down_revision: Union[str, Sequence[str], None] = "zz07d8e9f0a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAY15_TABLES = (
    "legacy_retirement_batches",
    "legacy_invoice_dispositions",
    "legacy_payment_dispositions",
    "legacy_subscription_financial_links",
    "legacy_retirement_audit",
)


def _enable_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE finance.{table_name} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE finance.{table_name} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table_name}
        ON finance.{table_name}
        FOR ALL
        USING (
            organization_id =
                NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        WITH CHECK (
            organization_id =
                NULLIF(current_setting('app.current_org_id', true), '')::uuid
        );
        """
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE finance.legacy_retirement_batches (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            scope_key VARCHAR(160) NOT NULL,
            status TEXT NOT NULL DEFAULT 'inventory',
            source_invoice_count BIGINT NOT NULL,
            source_payment_count BIGINT NOT NULL,
            source_subscription_link_count BIGINT NOT NULL,
            source_invoice_total NUMERIC(18,2) NOT NULL,
            source_invoice_tax_total NUMERIC(18,2) NOT NULL,
            source_payment_total NUMERIC(18,2) NOT NULL,
            source_checksum_sha256 CHAR(64) NOT NULL,
            manifest_sha256 CHAR(64) NULL,
            legacy_write_surfaces_disabled BOOLEAN NOT NULL DEFAULT TRUE,
            rollback_strategy TEXT NOT NULL
                DEFAULT 'finance_pause_legacy_read_only',
            cutover_at TIMESTAMPTZ NULL,
            cutover_by VARCHAR(160) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT uq_pay15_legacy_batch_scope
                UNIQUE (organization_id, scope_key),
            CONSTRAINT uq_pay15_legacy_batch_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT chk_pay15_legacy_batch_status
                CHECK (
                    status IN (
                        'inventory','reconciling','ready_for_cutover',
                        'cutover','rollback_hold'
                    )
                ),
            CONSTRAINT chk_pay15_legacy_batch_counts
                CHECK (
                    source_invoice_count >= 0
                    AND source_payment_count >= 0
                    AND source_subscription_link_count >= 0
                ),
            CONSTRAINT chk_pay15_legacy_batch_totals
                CHECK (
                    source_invoice_total >= 0
                    AND source_invoice_tax_total >= 0
                    AND source_payment_total >= 0
                ),
            CONSTRAINT chk_pay15_legacy_batch_source_checksum
                CHECK (source_checksum_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay15_legacy_batch_manifest_checksum
                CHECK (
                    manifest_sha256 IS NULL
                    OR manifest_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay15_legacy_batch_manifest_required
                CHECK (
                    status NOT IN (
                        'ready_for_cutover','cutover','rollback_hold'
                    )
                    OR manifest_sha256 IS NOT NULL
                ),
            CONSTRAINT chk_pay15_legacy_batch_cutover_shape
                CHECK (
                    status <> 'cutover'
                    OR (cutover_at IS NOT NULL AND cutover_by IS NOT NULL)
                ),
            CONSTRAINT chk_pay15_legacy_batch_write_surface
                CHECK (legacy_write_surfaces_disabled IS TRUE),
            CONSTRAINT chk_pay15_legacy_batch_rollback
                CHECK (rollback_strategy='finance_pause_legacy_read_only'),
            CONSTRAINT chk_pay15_legacy_batch_version CHECK (version >= 1)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE finance.legacy_invoice_dispositions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legacy_invoice_id UUID NOT NULL,
            finance_invoice_id UUID NULL
                REFERENCES finance.invoices(id) ON DELETE RESTRICT,
            legacy_invoice_number VARCHAR(200) NOT NULL,
            legacy_payment_id UUID NULL,
            legacy_subscription_id UUID NULL,
            disposition VARCHAR(40) NOT NULL,
            reconciliation_status VARCHAR(20) NOT NULL,
            source_subtotal NUMERIC(18,2) NOT NULL,
            source_discount_amount NUMERIC(18,2) NOT NULL,
            source_tax_amount NUMERIC(18,2) NOT NULL,
            source_total_amount NUMERIC(18,2) NOT NULL,
            source_currency_code CHAR(3) NOT NULL,
            source_status VARCHAR(40) NOT NULL,
            source_sha256 CHAR(64) NOT NULL,
            target_sha256 CHAR(64) NULL,
            reason_code VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_pay15_legacy_invoice_batch_org
                FOREIGN KEY (batch_id, organization_id)
                REFERENCES finance.legacy_retirement_batches(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay15_legacy_invoice_source
                UNIQUE (batch_id, legacy_invoice_id),
            CONSTRAINT uq_pay15_legacy_invoice_finance_target
                UNIQUE (finance_invoice_id),
            CONSTRAINT chk_pay15_legacy_invoice_disposition
                CHECK (
                    disposition IN (
                        'historical_read_only','migrated_finance'
                    )
                ),
            CONSTRAINT chk_pay15_legacy_invoice_exact
                CHECK (reconciliation_status='exact'),
            CONSTRAINT chk_pay15_legacy_invoice_migrated_target
                CHECK (
                    disposition <> 'migrated_finance'
                    OR finance_invoice_id IS NOT NULL
                ),
            CONSTRAINT chk_pay15_legacy_invoice_historical_no_target
                CHECK (
                    disposition <> 'historical_read_only'
                    OR finance_invoice_id IS NULL
                ),
            CONSTRAINT chk_pay15_legacy_invoice_amounts
                CHECK (
                    source_subtotal >= 0
                    AND source_discount_amount >= 0
                    AND source_tax_amount >= 0
                    AND source_total_amount >= 0
                ),
            CONSTRAINT chk_pay15_legacy_invoice_currency
                CHECK (source_currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_pay15_legacy_invoice_source_sha
                CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay15_legacy_invoice_target_sha
                CHECK (
                    target_sha256 IS NULL
                    OR target_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay15_legacy_invoice_reason
                CHECK (btrim(reason_code) <> '')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay15_legacy_invoice_org
        ON finance.legacy_invoice_dispositions
            (organization_id, legacy_invoice_id);
        """
    )

    op.execute(
        """
        CREATE TABLE finance.legacy_payment_dispositions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legacy_payment_id UUID NOT NULL,
            finance_payment_id UUID NULL
                REFERENCES finance.payments(id) ON DELETE RESTRICT,
            legacy_subscription_id UUID NULL,
            disposition VARCHAR(40) NOT NULL,
            reconciliation_status VARCHAR(20) NOT NULL,
            source_amount NUMERIC(18,2) NOT NULL,
            source_discount_amount NUMERIC(18,2) NOT NULL,
            source_currency_code CHAR(3) NOT NULL,
            source_status VARCHAR(40) NOT NULL,
            source_transaction_reference VARCHAR(300) NULL,
            source_razorpay_id VARCHAR(300) NULL,
            source_sha256 CHAR(64) NOT NULL,
            target_sha256 CHAR(64) NULL,
            reason_code VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_pay15_legacy_payment_batch_org
                FOREIGN KEY (batch_id, organization_id)
                REFERENCES finance.legacy_retirement_batches(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay15_legacy_payment_source
                UNIQUE (batch_id, legacy_payment_id),
            CONSTRAINT uq_pay15_legacy_payment_finance_target
                UNIQUE (finance_payment_id),
            CONSTRAINT chk_pay15_legacy_payment_disposition
                CHECK (
                    disposition IN (
                        'historical_read_only','migrated_finance'
                    )
                ),
            CONSTRAINT chk_pay15_legacy_payment_exact
                CHECK (reconciliation_status='exact'),
            CONSTRAINT chk_pay15_legacy_payment_migrated_target
                CHECK (
                    disposition <> 'migrated_finance'
                    OR finance_payment_id IS NOT NULL
                ),
            CONSTRAINT chk_pay15_legacy_payment_historical_no_target
                CHECK (
                    disposition <> 'historical_read_only'
                    OR finance_payment_id IS NULL
                ),
            CONSTRAINT chk_pay15_legacy_payment_amounts
                CHECK (
                    source_amount >= 0
                    AND source_discount_amount >= 0
                ),
            CONSTRAINT chk_pay15_legacy_payment_currency
                CHECK (source_currency_code ~ '^[A-Z]{3}$'),
            CONSTRAINT chk_pay15_legacy_payment_source_sha
                CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay15_legacy_payment_target_sha
                CHECK (
                    target_sha256 IS NULL
                    OR target_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_pay15_legacy_payment_reason
                CHECK (btrim(reason_code) <> '')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay15_legacy_payment_org
        ON finance.legacy_payment_dispositions
            (organization_id, legacy_payment_id);
        """
    )

    op.execute(
        """
        CREATE TABLE finance.legacy_subscription_financial_links (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            legacy_subscription_id UUID NOT NULL,
            modern_subscription_term_id UUID NULL,
            disposition VARCHAR(40) NOT NULL,
            source_sha256 CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_pay15_legacy_subscription_batch_org
                FOREIGN KEY (batch_id, organization_id)
                REFERENCES finance.legacy_retirement_batches(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay15_legacy_subscription_link_source
                UNIQUE (batch_id, legacy_subscription_id),
            CONSTRAINT chk_pay15_legacy_subscription_link_disposition
                CHECK (
                    disposition IN (
                        'historical_read_only',
                        'compatibility_projection',
                        'migrated_finance'
                    )
                ),
            CONSTRAINT chk_pay15_legacy_subscription_link_sha
                CHECK (source_sha256 ~ '^[0-9a-f]{64}$')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_pay15_legacy_subscription_link_org
        ON finance.legacy_subscription_financial_links
            (organization_id, legacy_subscription_id);
        """
    )

    op.execute(
        """
        CREATE TABLE finance.legacy_retirement_audit (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL,
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            sequence_number BIGINT NOT NULL,
            event_type VARCHAR(80) NOT NULL,
            actor_ref VARCHAR(160) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_pay15_legacy_audit_batch_org
                FOREIGN KEY (batch_id, organization_id)
                REFERENCES finance.legacy_retirement_batches(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_pay15_legacy_retirement_audit_sequence
                UNIQUE (batch_id, sequence_number),
            CONSTRAINT chk_pay15_legacy_retirement_audit_sequence
                CHECK (sequence_number > 0),
            CONSTRAINT chk_pay15_legacy_retirement_audit_event
                CHECK (
                    event_type IN (
                        'inventory_captured',
                        'invoice_dispositioned',
                        'payment_dispositioned',
                        'subscription_link_preserved',
                        'ready_certified',
                        'cutover_activated',
                        'rollback_hold_activated'
                    )
                ),
            CONSTRAINT chk_pay15_legacy_retirement_audit_actor
                CHECK (btrim(actor_ref) <> ''),
            CONSTRAINT chk_pay15_legacy_retirement_audit_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_pay15_legacy_retirement_audit_ref
                CHECK (btrim(evidence_ref) <> '')
        );
        """
    )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_block_legacy_monetary_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'PAY-15 legacy monetary authority retired: %.% is historical read-only',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE='55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_block_legacy_payments_write
        BEFORE INSERT OR UPDATE OR DELETE ON public.payments
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_block_legacy_monetary_write();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_block_legacy_invoices_write
        BEFORE INSERT OR UPDATE OR DELETE ON public.invoices
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_block_legacy_monetary_write();
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_pay15_block_legacy_payments_truncate
        BEFORE TRUNCATE ON public.payments
        FOR EACH STATEMENT
        EXECUTE FUNCTION finance.pay15_block_legacy_monetary_write();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_block_legacy_invoices_truncate
        BEFORE TRUNCATE ON public.invoices
        FOR EACH STATEMENT
        EXECUTE FUNCTION finance.pay15_block_legacy_monetary_write();
        """
    )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_protect_immutable_history()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION
                    'PAY-15 migration history is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    for table_name in (
        "legacy_invoice_dispositions",
        "legacy_payment_dispositions",
        "legacy_subscription_financial_links",
        "legacy_retirement_audit",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_pay15_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON finance.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION finance.pay15_protect_immutable_history();
            """
        )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_validate_batch_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.status <> 'inventory' THEN
                    RAISE EXCEPTION
                        'PAY-15 retirement batch must start inventory';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.scope_key,
                NEW.source_invoice_count, NEW.source_payment_count,
                NEW.source_subscription_link_count,
                NEW.source_invoice_total, NEW.source_invoice_tax_total,
                NEW.source_payment_total, NEW.source_checksum_sha256,
                NEW.created_at, NEW.rollback_strategy,
                NEW.legacy_write_surfaces_disabled
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.scope_key,
                OLD.source_invoice_count, OLD.source_payment_count,
                OLD.source_subscription_link_count,
                OLD.source_invoice_total, OLD.source_invoice_tax_total,
                OLD.source_payment_total, OLD.source_checksum_sha256,
                OLD.created_at, OLD.rollback_strategy,
                OLD.legacy_write_surfaces_disabled
            ) THEN
                RAISE EXCEPTION
                    'PAY-15 source inventory identity is immutable';
            END IF;

            IF OLD.status='rollback_hold'
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-15 rollback hold is terminal; legacy writes remain disabled';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='inventory' AND NEW.status='reconciling')
                    OR (
                        OLD.status='reconciling'
                        AND NEW.status='ready_for_cutover'
                    )
                    OR (
                        OLD.status='ready_for_cutover'
                        AND NEW.status IN ('cutover','reconciling')
                    )
                    OR (
                        OLD.status='cutover'
                        AND NEW.status='rollback_hold'
                    )
               ) THEN
                RAISE EXCEPTION
                    'PAY-15 forbidden batch transition: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_legacy_retirement_batch_lifecycle
        BEFORE INSERT OR UPDATE
        ON finance.legacy_retirement_batches
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_validate_batch_lifecycle();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_legacy_retirement_batch_touch
        BEFORE UPDATE ON finance.legacy_retirement_batches
        FOR EACH ROW
        EXECUTE FUNCTION public.platform_billing_touch_updated_at();
        """
    )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_validate_invoice_disposition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_batch_org UUID;
            v_batch_status TEXT;
            v_source RECORD;
            v_target RECORD;
            v_source_hash TEXT;
            v_target_hash TEXT;
        BEGIN
            SELECT organization_id, status
            INTO v_batch_org, v_batch_status
            FROM finance.legacy_retirement_batches
            WHERE id=NEW.batch_id;

            IF v_batch_org IS DISTINCT FROM NEW.organization_id
               OR v_batch_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 invoice disposition requires reconciling batch';
            END IF;

            SELECT
                i.id, g.org_id AS organization_id,
                i.invoice_number, i.payment_id, i.subscription_id,
                i.subtotal, i.discount_amount, i.tax_amount,
                i.total_amount, i.status::text AS status
            INTO v_source
            FROM public.invoices i
            JOIN public.gyms g ON g.id=i.gym_id
            WHERE i.id=NEW.legacy_invoice_id;

            IF v_source.id IS NULL
               OR v_source.organization_id IS DISTINCT FROM NEW.organization_id
               THEN
                RAISE EXCEPTION
                    'PAY-15 legacy invoice source is missing/cross-tenant';
            END IF;

            v_source_hash := encode(
                sha256(
                    convert_to(
                        concat_ws(
                            '|',
                            v_source.id::text,
                            v_source.organization_id::text,
                            v_source.invoice_number,
                            coalesce(v_source.payment_id::text,''),
                            coalesce(v_source.subscription_id::text,''),
                            v_source.subtotal::text,
                            v_source.discount_amount::text,
                            v_source.tax_amount::text,
                            v_source.total_amount::text,
                            v_source.status,
                            NEW.source_currency_code
                        ),
                        'utf8'
                    )
                ),
                'hex'
            );

            IF NEW.legacy_invoice_number IS DISTINCT FROM v_source.invoice_number
               OR NEW.legacy_payment_id IS DISTINCT FROM v_source.payment_id
               OR NEW.legacy_subscription_id IS DISTINCT FROM v_source.subscription_id
               OR NEW.source_subtotal IS DISTINCT FROM v_source.subtotal
               OR NEW.source_discount_amount IS DISTINCT FROM v_source.discount_amount
               OR NEW.source_tax_amount IS DISTINCT FROM v_source.tax_amount
               OR NEW.source_total_amount IS DISTINCT FROM v_source.total_amount
               OR NEW.source_status IS DISTINCT FROM v_source.status
               OR NEW.source_sha256 IS DISTINCT FROM v_source_hash THEN
                RAISE EXCEPTION
                    'PAY-15 legacy invoice snapshot mismatch';
            END IF;

            IF NEW.disposition='migrated_finance' THEN
                SELECT
                    id, organization_id, official_invoice_number,
                    subtotal_amount, discount_amount, total_tax_amount,
                    grand_total_amount, currency_code, status
                INTO v_target
                FROM finance.invoices
                WHERE id=NEW.finance_invoice_id;

                IF v_target.id IS NULL
                   OR v_target.organization_id IS DISTINCT FROM NEW.organization_id
                   OR v_target.official_invoice_number IS DISTINCT FROM v_source.invoice_number
                   OR v_target.subtotal_amount IS DISTINCT FROM v_source.subtotal
                   OR v_target.discount_amount IS DISTINCT FROM v_source.discount_amount
                   OR v_target.total_tax_amount IS DISTINCT FROM v_source.tax_amount
                   OR v_target.grand_total_amount IS DISTINCT FROM v_source.total_amount
                   OR v_target.currency_code IS DISTINCT FROM NEW.source_currency_code
                   THEN
                    RAISE EXCEPTION
                        'PAY-15 migrated Finance invoice does not exactly reconcile';
                END IF;

                v_target_hash := encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                v_target.id::text,
                                v_target.organization_id::text,
                                v_target.official_invoice_number,
                                v_target.subtotal_amount::text,
                                v_target.discount_amount::text,
                                v_target.total_tax_amount::text,
                                v_target.grand_total_amount::text,
                                v_target.currency_code,
                                v_target.status
                            ),
                            'utf8'
                        )
                    ),
                    'hex'
                );
                IF NEW.target_sha256 IS DISTINCT FROM v_target_hash THEN
                    RAISE EXCEPTION
                        'PAY-15 Finance invoice target checksum mismatch';
                END IF;
            ELSE
                IF NEW.finance_invoice_id IS NOT NULL
                   OR NEW.target_sha256 IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-15 historical invoice cannot bind Finance target';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_validate_invoice_disposition
        BEFORE INSERT
        ON finance.legacy_invoice_dispositions
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_validate_invoice_disposition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_validate_payment_disposition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_batch_org UUID;
            v_batch_status TEXT;
            v_source RECORD;
            v_target RECORD;
            v_source_hash TEXT;
            v_target_hash TEXT;
            v_source_ref TEXT;
        BEGIN
            SELECT organization_id, status
            INTO v_batch_org, v_batch_status
            FROM finance.legacy_retirement_batches
            WHERE id=NEW.batch_id;

            IF v_batch_org IS DISTINCT FROM NEW.organization_id
               OR v_batch_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 payment disposition requires reconciling batch';
            END IF;

            SELECT
                p.id, g.org_id AS organization_id,
                p.subscription_id, p.amount, p.discount_amount,
                p.status::text AS status,
                p.transaction_reference, p.razorpay_id
            INTO v_source
            FROM public.payments p
            JOIN public.gyms g ON g.id=p.gym_id
            WHERE p.id=NEW.legacy_payment_id;

            IF v_source.id IS NULL
               OR v_source.organization_id IS DISTINCT FROM NEW.organization_id
               THEN
                RAISE EXCEPTION
                    'PAY-15 legacy payment source is missing/cross-tenant';
            END IF;

            v_source_hash := encode(
                sha256(
                    convert_to(
                        concat_ws(
                            '|',
                            v_source.id::text,
                            v_source.organization_id::text,
                            coalesce(v_source.subscription_id::text,''),
                            v_source.amount::text,
                            v_source.discount_amount::text,
                            v_source.status,
                            coalesce(v_source.transaction_reference,''),
                            coalesce(v_source.razorpay_id,''),
                            NEW.source_currency_code
                        ),
                        'utf8'
                    )
                ),
                'hex'
            );

            IF NEW.legacy_subscription_id IS DISTINCT FROM v_source.subscription_id
               OR NEW.source_amount IS DISTINCT FROM v_source.amount
               OR NEW.source_discount_amount IS DISTINCT FROM v_source.discount_amount
               OR NEW.source_status IS DISTINCT FROM v_source.status
               OR NEW.source_transaction_reference IS DISTINCT FROM v_source.transaction_reference
               OR NEW.source_razorpay_id IS DISTINCT FROM v_source.razorpay_id
               OR NEW.source_sha256 IS DISTINCT FROM v_source_hash THEN
                RAISE EXCEPTION
                    'PAY-15 legacy payment snapshot mismatch';
            END IF;

            IF NEW.disposition='migrated_finance' THEN
                SELECT
                    id, organization_id, amount, currency_code,
                    status, provider_payment_ref
                INTO v_target
                FROM finance.payments
                WHERE id=NEW.finance_payment_id;

                v_source_ref := coalesce(
                    v_source.razorpay_id,
                    v_source.transaction_reference
                );

                IF v_target.id IS NULL
                   OR v_target.organization_id IS DISTINCT FROM NEW.organization_id
                   OR v_target.amount IS DISTINCT FROM v_source.amount
                   OR v_target.currency_code IS DISTINCT FROM NEW.source_currency_code
                   OR (
                        v_source_ref IS NOT NULL
                        AND v_target.provider_payment_ref IS DISTINCT FROM v_source_ref
                   )
                   THEN
                    RAISE EXCEPTION
                        'PAY-15 migrated Finance payment does not exactly reconcile';
                END IF;

                v_target_hash := encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                v_target.id::text,
                                v_target.organization_id::text,
                                v_target.amount::text,
                                v_target.currency_code,
                                v_target.status,
                                coalesce(v_target.provider_payment_ref,'')
                            ),
                            'utf8'
                        )
                    ),
                    'hex'
                );
                IF NEW.target_sha256 IS DISTINCT FROM v_target_hash THEN
                    RAISE EXCEPTION
                        'PAY-15 Finance payment target checksum mismatch';
                END IF;
            ELSE
                IF NEW.finance_payment_id IS NOT NULL
                   OR NEW.target_sha256 IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-15 historical payment cannot bind Finance target';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_validate_payment_disposition
        BEFORE INSERT
        ON finance.legacy_payment_dispositions
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_validate_payment_disposition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION finance.pay15_validate_subscription_link()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_batch_org UUID;
            v_batch_status TEXT;
            v_found BOOLEAN;
            v_source_hash TEXT;
        BEGIN
            SELECT organization_id, status
            INTO v_batch_org, v_batch_status
            FROM finance.legacy_retirement_batches
            WHERE id=NEW.batch_id;

            IF v_batch_org IS DISTINCT FROM NEW.organization_id
               OR v_batch_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 subscription link requires reconciling batch';
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM (
                    SELECT p.subscription_id
                    FROM public.payments p
                    JOIN public.gyms g ON g.id=p.gym_id
                    WHERE g.org_id=NEW.organization_id
                      AND p.subscription_id=NEW.legacy_subscription_id
                    UNION ALL
                    SELECT i.subscription_id
                    FROM public.invoices i
                    JOIN public.gyms g ON g.id=i.gym_id
                    WHERE g.org_id=NEW.organization_id
                      AND i.subscription_id=NEW.legacy_subscription_id
                ) s
                WHERE s.subscription_id IS NOT NULL
            ) INTO v_found;

            IF NOT v_found THEN
                RAISE EXCEPTION
                    'PAY-15 legacy subscription financial linkage does not exist';
            END IF;

            v_source_hash := encode(
                sha256(
                    convert_to(
                        concat_ws(
                            '|',
                            NEW.organization_id::text,
                            NEW.legacy_subscription_id::text,
                            NEW.disposition,
                            coalesce(NEW.modern_subscription_term_id::text,'')
                        ),
                        'utf8'
                    )
                ),
                'hex'
            );
            IF NEW.source_sha256 IS DISTINCT FROM v_source_hash THEN
                RAISE EXCEPTION
                    'PAY-15 subscription linkage checksum mismatch';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay15_validate_subscription_link
        BEFORE INSERT
        ON finance.legacy_subscription_financial_links
        FOR EACH ROW
        EXECUTE FUNCTION finance.pay15_validate_subscription_link();
        """
    )

    op.execute(
        """
        GRANT SELECT (
            id, gym_id, subscription_id, amount, discount_amount,
            status, transaction_reference, razorpay_id
        ) ON TABLE public.payments TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT (
            id, gym_id, invoice_number, payment_id, subscription_id,
            subtotal, discount_amount, tax_amount, total_amount, status
        ) ON TABLE public.invoices TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT (id, org_id)
        ON TABLE public.subscription_terms TO app_security_owner
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON TABLE
            finance.legacy_retirement_batches,
            finance.legacy_invoice_dispositions,
            finance.legacy_payment_dispositions,
            finance.legacy_subscription_financial_links,
            finance.legacy_retirement_audit
        TO app_security_owner
        """
    )
    op.execute("SET LOCAL ROLE app_security_owner")

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_source_snapshot_sha(
            p_organization_id uuid
        )
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
            SELECT encode(
                sha256(
                    convert_to(
                        coalesce(
                            string_agg(source_hash, '|' ORDER BY source_hash),
                            'empty'
                        ),
                        'utf8'
                    )
                ),
                'hex'
            )
            FROM (
                SELECT encode(
                    sha256(convert_to(concat_ws(
                        '|','invoice',i.id::text,g.org_id::text,i.invoice_number,
                        coalesce(i.payment_id::text,''),
                        coalesce(i.subscription_id::text,''),
                        i.subtotal::text,i.discount_amount::text,
                        i.tax_amount::text,i.total_amount::text,i.status::text
                    ),'utf8')),'hex') AS source_hash
                FROM public.invoices i
                JOIN public.gyms g ON g.id=i.gym_id
                WHERE g.org_id=p_organization_id
                UNION ALL
                SELECT encode(
                    sha256(convert_to(concat_ws(
                        '|','payment',p.id::text,g.org_id::text,
                        coalesce(p.subscription_id::text,''),
                        p.amount::text,p.discount_amount::text,p.status::text,
                        coalesce(p.transaction_reference,''),
                        coalesce(p.razorpay_id,'')
                    ),'utf8')),'hex')
                FROM public.payments p
                JOIN public.gyms g ON g.id=p.gym_id
                WHERE g.org_id=p_organization_id
                UNION ALL
                SELECT encode(
                    sha256(convert_to(concat_ws(
                        '|','subscription_link',p_organization_id::text,
                        subscription_id::text
                    ),'utf8')),'hex')
                FROM (
                    SELECT p.subscription_id
                    FROM public.payments p
                    JOIN public.gyms g ON g.id=p.gym_id
                    WHERE g.org_id=p_organization_id
                      AND p.subscription_id IS NOT NULL
                    UNION
                    SELECT i.subscription_id
                    FROM public.invoices i
                    JOIN public.gyms g ON g.id=i.gym_id
                    WHERE g.org_id=p_organization_id
                      AND i.subscription_id IS NOT NULL
                ) links
            ) source_hashes;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_capture_inventory(
            p_organization_id uuid,
            p_scope_key text,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_batch_id UUID := gen_random_uuid();
            v_invoice_count BIGINT;
            v_payment_count BIGINT;
            v_subscription_count BIGINT;
            v_invoice_total NUMERIC(18,2);
            v_invoice_tax_total NUMERIC(18,2);
            v_payment_total NUMERIC(18,2);
            v_checksum TEXT;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            IF NULLIF(current_setting('app.current_org_id', true), '')::uuid
               IS DISTINCT FROM p_organization_id THEN
                RAISE EXCEPTION 'PAY-15 tenant context mismatch';
            END IF;
            IF btrim(p_scope_key)=''
               OR btrim(p_actor_ref)=''
               OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION
                    'PAY-15 inventory requires scope, actor and evidence';
            END IF;
            SELECT
                count(*),
                coalesce(sum(i.total_amount),0),
                coalesce(sum(i.tax_amount),0)
            INTO v_invoice_count, v_invoice_total, v_invoice_tax_total
            FROM public.invoices i
            JOIN public.gyms g ON g.id=i.gym_id
            WHERE g.org_id=p_organization_id;

            SELECT count(*), coalesce(sum(p.amount),0)
            INTO v_payment_count, v_payment_total
            FROM public.payments p
            JOIN public.gyms g ON g.id=p.gym_id
            WHERE g.org_id=p_organization_id;

            SELECT count(DISTINCT subscription_id)
            INTO v_subscription_count
            FROM (
                SELECT p.subscription_id
                FROM public.payments p
                JOIN public.gyms g ON g.id=p.gym_id
                WHERE g.org_id=p_organization_id
                  AND p.subscription_id IS NOT NULL
                UNION
                SELECT i.subscription_id
                FROM public.invoices i
                JOIN public.gyms g ON g.id=i.gym_id
                WHERE g.org_id=p_organization_id
                  AND i.subscription_id IS NOT NULL
            ) source_links;

            SELECT encode(
                sha256(
                    convert_to(
                        coalesce(
                            string_agg(source_hash, '|' ORDER BY source_hash),
                            'empty'
                        ),
                        'utf8'
                    )
                ),
                'hex'
            )
            INTO v_checksum
            FROM (
                SELECT encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                'invoice',
                                i.id::text,
                                g.org_id::text,
                                i.invoice_number,
                                coalesce(i.payment_id::text,''),
                                coalesce(i.subscription_id::text,''),
                                i.subtotal::text,
                                i.discount_amount::text,
                                i.tax_amount::text,
                                i.total_amount::text,
                                i.status::text
                            ),
                            'utf8'
                        )
                    ),
                    'hex'
                ) AS source_hash
                FROM public.invoices i
                JOIN public.gyms g ON g.id=i.gym_id
                WHERE g.org_id=p_organization_id

                UNION ALL

                SELECT encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                'payment',
                                p.id::text,
                                g.org_id::text,
                                coalesce(p.subscription_id::text,''),
                                p.amount::text,
                                p.discount_amount::text,
                                p.status::text,
                                coalesce(p.transaction_reference,''),
                                coalesce(p.razorpay_id,'')
                            ),
                            'utf8'
                        )
                    ),
                    'hex'
                )
                FROM public.payments p
                JOIN public.gyms g ON g.id=p.gym_id
                WHERE g.org_id=p_organization_id

                UNION ALL

                SELECT encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                '|',
                                'subscription_link',
                                p_organization_id::text,
                                subscription_id::text
                            ),
                            'utf8'
                        )
                    ),
                    'hex'
                )
                FROM (
                    SELECT p.subscription_id
                    FROM public.payments p
                    JOIN public.gyms g ON g.id=p.gym_id
                    WHERE g.org_id=p_organization_id
                      AND p.subscription_id IS NOT NULL
                    UNION
                    SELECT i.subscription_id
                    FROM public.invoices i
                    JOIN public.gyms g ON g.id=i.gym_id
                    WHERE g.org_id=p_organization_id
                      AND i.subscription_id IS NOT NULL
                ) links
            ) source_hashes;

            INSERT INTO finance.legacy_retirement_batches(
                id, organization_id, scope_key, status,
                source_invoice_count, source_payment_count,
                source_subscription_link_count,
                source_invoice_total, source_invoice_tax_total,
                source_payment_total, source_checksum_sha256,
                legacy_write_surfaces_disabled, rollback_strategy
            ) VALUES (
                v_batch_id, p_organization_id, p_scope_key, 'inventory',
                v_invoice_count, v_payment_count, v_subscription_count,
                v_invoice_total, v_invoice_tax_total,
                v_payment_total, v_checksum,
                true, 'finance_pause_legacy_read_only'
            );

            INSERT INTO finance.legacy_retirement_audit(
                batch_id, organization_id, sequence_number,
                event_type, actor_ref, evidence_sha256, evidence_ref
            ) VALUES (
                v_batch_id, p_organization_id, 1,
                'inventory_captured', p_actor_ref, v_checksum, p_evidence_ref
            );

            RETURN v_batch_id;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_begin_reconciliation(p_batch_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE v_org uuid;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            SELECT organization_id INTO v_org
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id FOR UPDATE;
            IF v_org IS NULL
               OR NULLIF(current_setting('app.current_org_id', true), '')::uuid
                  IS DISTINCT FROM v_org THEN
                RAISE EXCEPTION 'PAY-15 batch tenant context mismatch';
            END IF;
            UPDATE finance.legacy_retirement_batches
            SET status='reconciling',version=version+1
            WHERE id=p_batch_id AND status='inventory';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'PAY-15 batch is not eligible for reconciliation';
            END IF;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_record_invoice_disposition(
            p_batch_id uuid,
            p_legacy_invoice_id uuid,
            p_disposition text,
            p_finance_invoice_id uuid,
            p_source_currency_code text,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_org uuid;
            v_status text;
            v_source record;
            v_target record;
            v_source_hash text;
            v_target_hash text;
            v_id uuid;
            v_seq bigint;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            SELECT organization_id,status INTO v_org,v_status
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id FOR UPDATE;
            IF v_org IS NULL
               OR NULLIF(current_setting('app.current_org_id', true), '')::uuid
                  IS DISTINCT FROM v_org
               OR v_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 invoice disposition batch is not reconciling for tenant';
            END IF;
            IF p_disposition NOT IN ('historical_read_only','migrated_finance')
               OR p_source_currency_code !~ '^[A-Z]{3}$'
               OR btrim(p_actor_ref)='' OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION 'PAY-15 invalid invoice disposition command';
            END IF;

            SELECT i.id,g.org_id AS organization_id,i.invoice_number,
                   i.payment_id,i.subscription_id,i.subtotal,i.discount_amount,
                   i.tax_amount,i.total_amount,i.status::text AS status
            INTO v_source
            FROM public.invoices i
            JOIN public.gyms g ON g.id=i.gym_id
            WHERE i.id=p_legacy_invoice_id AND g.org_id=v_org;
            IF v_source.id IS NULL THEN
                RAISE EXCEPTION 'PAY-15 legacy invoice source is unknown';
            END IF;

            v_source_hash := encode(sha256(convert_to(concat_ws(
                '|',v_source.id::text,v_source.organization_id::text,
                v_source.invoice_number,coalesce(v_source.payment_id::text,''),
                coalesce(v_source.subscription_id::text,''),
                v_source.subtotal::text,v_source.discount_amount::text,
                v_source.tax_amount::text,v_source.total_amount::text,
                v_source.status,p_source_currency_code
            ),'utf8')),'hex');

            IF p_disposition='migrated_finance' THEN
                IF p_finance_invoice_id IS NULL THEN
                    RAISE EXCEPTION 'PAY-15 migrated invoice requires Finance target';
                END IF;
                SELECT id,organization_id,official_invoice_number,subtotal_amount,
                       discount_amount,total_tax_amount,grand_total_amount,
                       currency_code,status
                INTO v_target
                FROM finance.invoices
                WHERE id=p_finance_invoice_id;
                IF v_target.id IS NULL THEN
                    RAISE EXCEPTION 'PAY-15 Finance invoice target is unknown';
                END IF;
                v_target_hash := encode(sha256(convert_to(concat_ws(
                    '|',v_target.id::text,v_target.organization_id::text,
                    v_target.official_invoice_number,v_target.subtotal_amount::text,
                    v_target.discount_amount::text,v_target.total_tax_amount::text,
                    v_target.grand_total_amount::text,v_target.currency_code,
                    v_target.status
                ),'utf8')),'hex');
            ELSE
                IF p_finance_invoice_id IS NOT NULL THEN
                    RAISE EXCEPTION 'PAY-15 historical invoice cannot bind Finance target';
                END IF;
                v_target_hash := NULL;
            END IF;

            INSERT INTO finance.legacy_invoice_dispositions(
                batch_id,organization_id,legacy_invoice_id,finance_invoice_id,
                legacy_invoice_number,legacy_payment_id,legacy_subscription_id,
                disposition,reconciliation_status,source_subtotal,
                source_discount_amount,source_tax_amount,source_total_amount,
                source_currency_code,source_status,source_sha256,target_sha256,
                reason_code
            ) VALUES (
                p_batch_id,v_org,v_source.id,p_finance_invoice_id,
                v_source.invoice_number,v_source.payment_id,v_source.subscription_id,
                p_disposition,'exact',v_source.subtotal,v_source.discount_amount,
                v_source.tax_amount,v_source.total_amount,p_source_currency_code,
                v_source.status,v_source_hash,v_target_hash,
                CASE WHEN p_disposition='migrated_finance'
                     THEN 'PAY15_FINANCE_INVOICE_EXACT'
                     ELSE 'PAY15_HISTORICAL_READ_ONLY' END
            ) RETURNING id INTO v_id;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;
            INSERT INTO finance.legacy_retirement_audit(
                batch_id,organization_id,sequence_number,event_type,actor_ref,
                evidence_sha256,evidence_ref
            ) VALUES (
                p_batch_id,v_org,v_seq,'invoice_dispositioned',p_actor_ref,
                v_source_hash,p_evidence_ref
            );
            RETURN v_id;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_record_payment_disposition(
            p_batch_id uuid,
            p_legacy_payment_id uuid,
            p_disposition text,
            p_finance_payment_id uuid,
            p_source_currency_code text,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_org uuid;
            v_status text;
            v_source record;
            v_target record;
            v_source_hash text;
            v_target_hash text;
            v_id uuid;
            v_seq bigint;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            SELECT organization_id,status INTO v_org,v_status
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id FOR UPDATE;
            IF v_org IS NULL
               OR NULLIF(current_setting('app.current_org_id', true), '')::uuid
                  IS DISTINCT FROM v_org
               OR v_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 payment disposition batch is not reconciling for tenant';
            END IF;
            IF p_disposition NOT IN ('historical_read_only','migrated_finance')
               OR p_source_currency_code !~ '^[A-Z]{3}$'
               OR btrim(p_actor_ref)='' OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION 'PAY-15 invalid payment disposition command';
            END IF;

            SELECT p.id,g.org_id AS organization_id,p.subscription_id,p.amount,
                   p.discount_amount,p.status::text AS status,
                   p.transaction_reference,p.razorpay_id
            INTO v_source
            FROM public.payments p
            JOIN public.gyms g ON g.id=p.gym_id
            WHERE p.id=p_legacy_payment_id AND g.org_id=v_org;
            IF v_source.id IS NULL THEN
                RAISE EXCEPTION 'PAY-15 legacy payment source is unknown';
            END IF;

            v_source_hash := encode(sha256(convert_to(concat_ws(
                '|',v_source.id::text,v_source.organization_id::text,
                coalesce(v_source.subscription_id::text,''),
                v_source.amount::text,v_source.discount_amount::text,
                v_source.status,coalesce(v_source.transaction_reference,''),
                coalesce(v_source.razorpay_id,''),p_source_currency_code
            ),'utf8')),'hex');

            IF p_disposition='migrated_finance' THEN
                IF p_finance_payment_id IS NULL THEN
                    RAISE EXCEPTION 'PAY-15 migrated payment requires Finance target';
                END IF;
                SELECT id,organization_id,amount,currency_code,status,
                       provider_payment_ref
                INTO v_target
                FROM finance.payments
                WHERE id=p_finance_payment_id;
                IF v_target.id IS NULL THEN
                    RAISE EXCEPTION 'PAY-15 Finance payment target is unknown';
                END IF;
                v_target_hash := encode(sha256(convert_to(concat_ws(
                    '|',v_target.id::text,v_target.organization_id::text,
                    v_target.amount::text,v_target.currency_code,v_target.status,
                    coalesce(v_target.provider_payment_ref,'')
                ),'utf8')),'hex');
            ELSE
                IF p_finance_payment_id IS NOT NULL THEN
                    RAISE EXCEPTION 'PAY-15 historical payment cannot bind Finance target';
                END IF;
                v_target_hash := NULL;
            END IF;

            INSERT INTO finance.legacy_payment_dispositions(
                batch_id,organization_id,legacy_payment_id,finance_payment_id,
                legacy_subscription_id,disposition,reconciliation_status,
                source_amount,source_discount_amount,source_currency_code,
                source_status,source_transaction_reference,source_razorpay_id,
                source_sha256,target_sha256,reason_code
            ) VALUES (
                p_batch_id,v_org,v_source.id,p_finance_payment_id,
                v_source.subscription_id,p_disposition,'exact',v_source.amount,
                v_source.discount_amount,p_source_currency_code,v_source.status,
                v_source.transaction_reference,v_source.razorpay_id,
                v_source_hash,v_target_hash,
                CASE WHEN p_disposition='migrated_finance'
                     THEN 'PAY15_FINANCE_PAYMENT_EXACT'
                     ELSE 'PAY15_HISTORICAL_READ_ONLY' END
            ) RETURNING id INTO v_id;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;
            INSERT INTO finance.legacy_retirement_audit(
                batch_id,organization_id,sequence_number,event_type,actor_ref,
                evidence_sha256,evidence_ref
            ) VALUES (
                p_batch_id,v_org,v_seq,'payment_dispositioned',p_actor_ref,
                v_source_hash,p_evidence_ref
            );
            RETURN v_id;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_record_subscription_link(
            p_batch_id uuid,
            p_legacy_subscription_id uuid,
            p_disposition text,
            p_modern_subscription_term_id uuid,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_org uuid;
            v_status text;
            v_hash text;
            v_id uuid;
            v_seq bigint;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            SELECT organization_id,status INTO v_org,v_status
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id FOR UPDATE;
            IF v_org IS NULL
               OR NULLIF(current_setting('app.current_org_id', true), '')::uuid
                  IS DISTINCT FROM v_org
               OR v_status <> 'reconciling' THEN
                RAISE EXCEPTION
                    'PAY-15 subscription linkage batch is not reconciling for tenant';
            END IF;
            IF p_disposition NOT IN (
                'historical_read_only','compatibility_projection','migrated_finance'
            ) OR btrim(p_actor_ref)='' OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION 'PAY-15 invalid subscription linkage command';
            END IF;
            IF p_modern_subscription_term_id IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1 FROM public.subscription_terms st
                    WHERE st.id=p_modern_subscription_term_id AND st.org_id=v_org
               ) THEN
                RAISE EXCEPTION
                    'PAY-15 modern subscription term is unknown/cross-tenant';
            END IF;

            v_hash := encode(sha256(convert_to(concat_ws(
                '|',v_org::text,p_legacy_subscription_id::text,p_disposition,
                coalesce(p_modern_subscription_term_id::text,'')
            ),'utf8')),'hex');

            INSERT INTO finance.legacy_subscription_financial_links(
                batch_id,organization_id,legacy_subscription_id,
                modern_subscription_term_id,disposition,source_sha256
            ) VALUES (
                p_batch_id,v_org,p_legacy_subscription_id,
                p_modern_subscription_term_id,p_disposition,v_hash
            ) RETURNING id INTO v_id;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;
            INSERT INTO finance.legacy_retirement_audit(
                batch_id,organization_id,sequence_number,event_type,actor_ref,
                evidence_sha256,evidence_ref
            ) VALUES (
                p_batch_id,v_org,v_seq,'subscription_link_preserved',p_actor_ref,
                v_hash,p_evidence_ref
            );
            RETURN v_id;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_assert_reconciled(p_batch_id uuid)
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_batch finance.legacy_retirement_batches%ROWTYPE;
            v_invoice_count BIGINT;
            v_payment_count BIGINT;
            v_subscription_count BIGINT;
            v_invoice_total NUMERIC(18,2);
            v_invoice_tax_total NUMERIC(18,2);
            v_payment_total NUMERIC(18,2);
            v_missing_invoices BIGINT;
            v_missing_payments BIGINT;
            v_missing_links BIGINT;
            v_invoice_snapshot_mismatch BIGINT;
            v_payment_snapshot_mismatch BIGINT;
            v_finance_invoice_mismatch BIGINT;
            v_finance_payment_mismatch BIGINT;
            v_duplicate_targets BIGINT;
            v_disposition_invoice_total NUMERIC(18,2);
            v_disposition_invoice_tax_total NUMERIC(18,2);
            v_disposition_payment_total NUMERIC(18,2);
            v_source_snapshot_sha TEXT;
            v_manifest TEXT;
        BEGIN
            SELECT * INTO v_batch
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id
            FOR UPDATE;

            IF v_batch.id IS NULL THEN
                RAISE EXCEPTION 'PAY-15 retirement batch not found';
            END IF;
            IF NULLIF(current_setting('app.current_org_id', true), '')::uuid
               IS DISTINCT FROM v_batch.organization_id THEN
                RAISE EXCEPTION 'PAY-15 reconciliation tenant context mismatch';
            END IF;
            IF v_batch.status NOT IN ('reconciling','ready_for_cutover') THEN
                RAISE EXCEPTION
                    'PAY-15 reconciliation assertion requires reconciling/ready batch';
            END IF;
            IF NOT v_batch.legacy_write_surfaces_disabled THEN
                RAISE EXCEPTION 'PAY-15 legacy write surfaces are not disabled';
            END IF;

            SELECT
                count(*),
                coalesce(sum(i.total_amount),0),
                coalesce(sum(i.tax_amount),0)
            INTO v_invoice_count, v_invoice_total, v_invoice_tax_total
            FROM public.invoices i
            JOIN public.gyms g ON g.id=i.gym_id
            WHERE g.org_id=v_batch.organization_id;

            SELECT count(*), coalesce(sum(p.amount),0)
            INTO v_payment_count, v_payment_total
            FROM public.payments p
            JOIN public.gyms g ON g.id=p.gym_id
            WHERE g.org_id=v_batch.organization_id;

            SELECT count(DISTINCT subscription_id)
            INTO v_subscription_count
            FROM (
                SELECT p.subscription_id
                FROM public.payments p
                JOIN public.gyms g ON g.id=p.gym_id
                WHERE g.org_id=v_batch.organization_id
                  AND p.subscription_id IS NOT NULL
                UNION
                SELECT i.subscription_id
                FROM public.invoices i
                JOIN public.gyms g ON g.id=i.gym_id
                WHERE g.org_id=v_batch.organization_id
                  AND i.subscription_id IS NOT NULL
            ) s;

            v_source_snapshot_sha :=
                app_secure.pay15_source_snapshot_sha(v_batch.organization_id);
            IF v_source_snapshot_sha IS DISTINCT FROM v_batch.source_checksum_sha256 THEN
                RAISE EXCEPTION
                    'PAY-15 source checksum changed after inventory snapshot';
            END IF;

            IF v_invoice_count <> v_batch.source_invoice_count
               OR v_payment_count <> v_batch.source_payment_count
               OR v_subscription_count <> v_batch.source_subscription_link_count
               OR v_invoice_total IS DISTINCT FROM v_batch.source_invoice_total
               OR v_invoice_tax_total IS DISTINCT FROM v_batch.source_invoice_tax_total
               OR v_payment_total IS DISTINCT FROM v_batch.source_payment_total THEN
                RAISE EXCEPTION
                    'PAY-15 source changed after inventory snapshot';
            END IF;

            SELECT count(*) INTO v_missing_invoices
            FROM public.invoices i
            JOIN public.gyms g ON g.id=i.gym_id
            LEFT JOIN finance.legacy_invoice_dispositions d
              ON d.batch_id=v_batch.id
             AND d.legacy_invoice_id=i.id
            WHERE g.org_id=v_batch.organization_id
              AND d.id IS NULL;

            SELECT count(*) INTO v_missing_payments
            FROM public.payments p
            JOIN public.gyms g ON g.id=p.gym_id
            LEFT JOIN finance.legacy_payment_dispositions d
              ON d.batch_id=v_batch.id
             AND d.legacy_payment_id=p.id
            WHERE g.org_id=v_batch.organization_id
              AND d.id IS NULL;

            SELECT count(*) INTO v_missing_links
            FROM (
                SELECT p.subscription_id
                FROM public.payments p
                JOIN public.gyms g ON g.id=p.gym_id
                WHERE g.org_id=v_batch.organization_id
                  AND p.subscription_id IS NOT NULL
                UNION
                SELECT i.subscription_id
                FROM public.invoices i
                JOIN public.gyms g ON g.id=i.gym_id
                WHERE g.org_id=v_batch.organization_id
                  AND i.subscription_id IS NOT NULL
            ) s
            LEFT JOIN finance.legacy_subscription_financial_links l
              ON l.batch_id=v_batch.id
             AND l.legacy_subscription_id=s.subscription_id
            WHERE l.id IS NULL;

            SELECT count(*) INTO v_invoice_snapshot_mismatch
            FROM finance.legacy_invoice_dispositions d
            JOIN public.invoices i ON i.id=d.legacy_invoice_id
            JOIN public.gyms g ON g.id=i.gym_id
            WHERE d.batch_id=v_batch.id
              AND (
                    g.org_id IS DISTINCT FROM d.organization_id
                    OR i.invoice_number IS DISTINCT FROM d.legacy_invoice_number
                    OR i.payment_id IS DISTINCT FROM d.legacy_payment_id
                    OR i.subscription_id IS DISTINCT FROM d.legacy_subscription_id
                    OR i.subtotal IS DISTINCT FROM d.source_subtotal
                    OR i.discount_amount IS DISTINCT FROM d.source_discount_amount
                    OR i.tax_amount IS DISTINCT FROM d.source_tax_amount
                    OR i.total_amount IS DISTINCT FROM d.source_total_amount
                    OR i.status::text IS DISTINCT FROM d.source_status
              );

            SELECT count(*) INTO v_payment_snapshot_mismatch
            FROM finance.legacy_payment_dispositions d
            JOIN public.payments p ON p.id=d.legacy_payment_id
            JOIN public.gyms g ON g.id=p.gym_id
            WHERE d.batch_id=v_batch.id
              AND (
                    g.org_id IS DISTINCT FROM d.organization_id
                    OR p.subscription_id IS DISTINCT FROM d.legacy_subscription_id
                    OR p.amount IS DISTINCT FROM d.source_amount
                    OR p.discount_amount IS DISTINCT FROM d.source_discount_amount
                    OR p.status::text IS DISTINCT FROM d.source_status
                    OR p.transaction_reference IS DISTINCT FROM d.source_transaction_reference
                    OR p.razorpay_id IS DISTINCT FROM d.source_razorpay_id
              );

            SELECT count(*) INTO v_finance_invoice_mismatch
            FROM finance.legacy_invoice_dispositions d
            LEFT JOIN finance.invoices f ON f.id=d.finance_invoice_id
            WHERE d.batch_id=v_batch.id
              AND d.disposition='migrated_finance'
              AND (
                    f.id IS NULL
                    OR f.organization_id IS DISTINCT FROM d.organization_id
                    OR f.official_invoice_number IS DISTINCT FROM d.legacy_invoice_number
                    OR f.subtotal_amount IS DISTINCT FROM d.source_subtotal
                    OR f.discount_amount IS DISTINCT FROM d.source_discount_amount
                    OR f.total_tax_amount IS DISTINCT FROM d.source_tax_amount
                    OR f.grand_total_amount IS DISTINCT FROM d.source_total_amount
                    OR f.currency_code IS DISTINCT FROM d.source_currency_code
              );

            SELECT count(*) INTO v_finance_payment_mismatch
            FROM finance.legacy_payment_dispositions d
            LEFT JOIN finance.payments f ON f.id=d.finance_payment_id
            WHERE d.batch_id=v_batch.id
              AND d.disposition='migrated_finance'
              AND (
                    f.id IS NULL
                    OR f.organization_id IS DISTINCT FROM d.organization_id
                    OR f.amount IS DISTINCT FROM d.source_amount
                    OR f.currency_code IS DISTINCT FROM d.source_currency_code
                    OR (
                        coalesce(
                            d.source_razorpay_id,
                            d.source_transaction_reference
                        ) IS NOT NULL
                        AND f.provider_payment_ref IS DISTINCT FROM coalesce(
                            d.source_razorpay_id,
                            d.source_transaction_reference
                        )
                    )
              );

            SELECT count(*) INTO v_duplicate_targets
            FROM (
                SELECT finance_invoice_id AS target_id
                FROM finance.legacy_invoice_dispositions
                WHERE batch_id=v_batch.id
                  AND finance_invoice_id IS NOT NULL
                GROUP BY finance_invoice_id
                HAVING count(*) > 1
                UNION ALL
                SELECT finance_payment_id AS target_id
                FROM finance.legacy_payment_dispositions
                WHERE batch_id=v_batch.id
                  AND finance_payment_id IS NOT NULL
                GROUP BY finance_payment_id
                HAVING count(*) > 1
            ) duplicates;

            SELECT
                coalesce(sum(source_total_amount),0),
                coalesce(sum(source_tax_amount),0)
            INTO v_disposition_invoice_total, v_disposition_invoice_tax_total
            FROM finance.legacy_invoice_dispositions
            WHERE batch_id=v_batch.id;

            SELECT coalesce(sum(source_amount),0)
            INTO v_disposition_payment_total
            FROM finance.legacy_payment_dispositions
            WHERE batch_id=v_batch.id;

            IF v_missing_invoices <> 0 THEN
                RAISE EXCEPTION
                    'PAY-15 unknown historical invoices=%',
                    v_missing_invoices;
            END IF;
            IF v_missing_payments <> 0 THEN
                RAISE EXCEPTION
                    'PAY-15 unreconciled legacy payments=%',
                    v_missing_payments;
            END IF;
            IF v_missing_links <> 0 THEN
                RAISE EXCEPTION
                    'PAY-15 unpreserved subscription bindings=%',
                    v_missing_links;
            END IF;
            IF v_invoice_snapshot_mismatch <> 0
               OR v_payment_snapshot_mismatch <> 0
               OR v_finance_invoice_mismatch <> 0
               OR v_finance_payment_mismatch <> 0 THEN
                RAISE EXCEPTION
                    'PAY-15 unreconciled migrated money remains';
            END IF;
            IF v_duplicate_targets <> 0 THEN
                RAISE EXCEPTION
                    'PAY-15 duplicate Finance records=%',
                    v_duplicate_targets;
            END IF;
            IF v_disposition_invoice_total IS DISTINCT FROM v_batch.source_invoice_total
               OR v_disposition_invoice_tax_total IS DISTINCT FROM v_batch.source_invoice_tax_total
               OR v_disposition_payment_total IS DISTINCT FROM v_batch.source_payment_total
               THEN
                RAISE EXCEPTION
                    'PAY-15 monetary/tax totals do not reconcile';
            END IF;

            SELECT encode(
                sha256(
                    convert_to(
                        coalesce(
                            string_agg(hash_value, '|' ORDER BY hash_value),
                            'empty'
                        ),
                        'utf8'
                    )
                ),
                'hex'
            )
            INTO v_manifest
            FROM (
                SELECT source_sha256 AS hash_value
                FROM finance.legacy_invoice_dispositions
                WHERE batch_id=v_batch.id
                UNION ALL
                SELECT source_sha256
                FROM finance.legacy_payment_dispositions
                WHERE batch_id=v_batch.id
                UNION ALL
                SELECT source_sha256
                FROM finance.legacy_subscription_financial_links
                WHERE batch_id=v_batch.id
            ) hashes;

            RETURN v_manifest;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_certify_ready(
            p_batch_id uuid,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_manifest TEXT;
            v_org UUID;
            v_seq BIGINT;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            IF btrim(p_actor_ref)='' OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION 'PAY-15 certification requires actor/evidence';
            END IF;

            v_manifest := app_secure.pay15_assert_reconciled(p_batch_id);

            SELECT organization_id INTO v_org
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id
            FOR UPDATE;

            UPDATE finance.legacy_retirement_batches
            SET status='ready_for_cutover',
                manifest_sha256=v_manifest,
                version=version+1
            WHERE id=p_batch_id
              AND status='reconciling';

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'PAY-15 batch must be reconciling before readiness';
            END IF;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;

            INSERT INTO finance.legacy_retirement_audit(
                batch_id, organization_id, sequence_number,
                event_type, actor_ref, evidence_sha256, evidence_ref
            ) VALUES (
                p_batch_id, v_org, v_seq,
                'ready_certified', p_actor_ref, v_manifest, p_evidence_ref
            );

            RETURN v_manifest;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_activate_cutover(
            p_batch_id uuid,
            p_expected_manifest_sha256 text,
            p_actor_ref text,
            p_evidence_ref text
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_manifest TEXT;
            v_org UUID;
            v_seq BIGINT;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            SELECT organization_id, manifest_sha256
            INTO v_org, v_manifest
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id
              AND status='ready_for_cutover'
            FOR UPDATE;

            IF v_org IS NULL THEN
                RAISE EXCEPTION
                    'PAY-15 cutover requires ready_for_cutover batch';
            END IF;
            IF NULLIF(current_setting('app.current_org_id', true), '')::uuid
               IS DISTINCT FROM v_org THEN
                RAISE EXCEPTION 'PAY-15 cutover tenant context mismatch';
            END IF;
            IF v_manifest IS DISTINCT FROM p_expected_manifest_sha256 THEN
                RAISE EXCEPTION
                    'PAY-15 cutover manifest checksum mismatch';
            END IF;
            IF app_secure.pay15_assert_reconciled(p_batch_id)
               IS DISTINCT FROM v_manifest THEN
                RAISE EXCEPTION
                    'PAY-15 reconciliation changed after readiness';
            END IF;

            UPDATE finance.legacy_retirement_batches
            SET status='cutover',
                cutover_at=clock_timestamp(),
                cutover_by=p_actor_ref,
                version=version+1
            WHERE id=p_batch_id;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;

            INSERT INTO finance.legacy_retirement_audit(
                batch_id, organization_id, sequence_number,
                event_type, actor_ref, evidence_sha256, evidence_ref
            ) VALUES (
                p_batch_id, v_org, v_seq,
                'cutover_activated', p_actor_ref, v_manifest, p_evidence_ref
            );
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.pay15_enter_rollback_hold(
            p_batch_id uuid,
            p_actor_ref text,
            p_evidence_sha256 text,
            p_evidence_ref text
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, finance
        SET row_security = on
        AS $$
        DECLARE
            v_org UUID;
            v_seq BIGINT;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'finance_config_runtime', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'PAY-15 migration requires finance_config_runtime'
                    USING ERRCODE='42501';
            END IF;
            IF p_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR btrim(p_evidence_ref)='' THEN
                RAISE EXCEPTION
                    'PAY-15 rollback hold requires evidence';
            END IF;

            SELECT organization_id INTO v_org
            FROM finance.legacy_retirement_batches
            WHERE id=p_batch_id
              AND status='cutover'
            FOR UPDATE;

            IF v_org IS NULL THEN
                RAISE EXCEPTION
                    'PAY-15 rollback hold requires cutover batch';
            END IF;
            IF NULLIF(current_setting('app.current_org_id', true), '')::uuid
               IS DISTINCT FROM v_org THEN
                RAISE EXCEPTION 'PAY-15 rollback hold tenant context mismatch';
            END IF;

            UPDATE finance.legacy_retirement_batches
            SET status='rollback_hold', version=version+1
            WHERE id=p_batch_id;

            SELECT coalesce(max(sequence_number),0)+1 INTO v_seq
            FROM finance.legacy_retirement_audit
            WHERE batch_id=p_batch_id;

            INSERT INTO finance.legacy_retirement_audit(
                batch_id, organization_id, sequence_number,
                event_type, actor_ref, evidence_sha256, evidence_ref
            ) VALUES (
                p_batch_id, v_org, v_seq,
                'rollback_hold_activated',
                p_actor_ref, p_evidence_sha256, p_evidence_ref
            );
        END;
        $$;
        """
    )

    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_source_snapshot_sha(uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_capture_inventory(uuid,text,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_begin_reconciliation(uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_record_invoice_disposition(uuid,uuid,text,uuid,text,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_record_payment_disposition(uuid,uuid,text,uuid,text,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_record_subscription_link(uuid,uuid,text,uuid,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_assert_reconciled(uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_certify_ready(uuid,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_activate_cutover(uuid,text,text,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.pay15_enter_rollback_hold(uuid,text,text,text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_capture_inventory(uuid,text,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_begin_reconciliation(uuid) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_record_invoice_disposition(uuid,uuid,text,uuid,text,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_record_payment_disposition(uuid,uuid,text,uuid,text,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_record_subscription_link(uuid,uuid,text,uuid,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_certify_ready(uuid,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_activate_cutover(uuid,text,text,text) TO finance_config_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.pay15_enter_rollback_hold(uuid,text,text,text) TO finance_config_runtime")
    op.execute("RESET ROLE")

    op.execute(
        """
        REVOKE ALL ON TABLE
            finance.legacy_retirement_batches,
            finance.legacy_invoice_dispositions,
            finance.legacy_payment_dispositions,
            finance.legacy_subscription_financial_links,
            finance.legacy_retirement_audit
        FROM PUBLIC, app_runtime, auth_runtime, worker_runtime,
             lifecycle_maintenance_runtime, finance_config_runtime;
        """
    )

    op.execute(
        """
        CREATE VIEW finance.legacy_invoices_compat_v
        WITH (security_barrier=true)
        AS
        SELECT
            i.id AS legacy_invoice_id,
            g.org_id AS organization_id,
            i.invoice_number,
            i.payment_id AS legacy_payment_id,
            i.subscription_id AS legacy_subscription_id,
            i.subtotal,
            i.discount_amount,
            i.tax_amount,
            i.total_amount,
            i.status::text AS legacy_status,
            i.issued_at,
            d.disposition,
            d.finance_invoice_id,
            d.reconciliation_status,
            d.source_sha256
        FROM public.invoices i
        JOIN public.gyms g ON g.id=i.gym_id
        LEFT JOIN finance.legacy_invoice_dispositions d
          ON d.legacy_invoice_id=i.id
         AND d.organization_id=g.org_id
        WHERE g.org_id =
            NULLIF(current_setting('app.current_org_id', true), '')::uuid;
        """
    )
    op.execute(
        """
        CREATE VIEW finance.legacy_payments_compat_v
        WITH (security_barrier=true)
        AS
        SELECT
            p.id AS legacy_payment_id,
            g.org_id AS organization_id,
            p.subscription_id AS legacy_subscription_id,
            p.amount,
            p.discount_amount,
            p.status::text AS legacy_status,
            p.transaction_reference,
            p.razorpay_id,
            p.payment_date,
            d.disposition,
            d.finance_payment_id,
            d.reconciliation_status,
            d.source_sha256
        FROM public.payments p
        JOIN public.gyms g ON g.id=p.gym_id
        LEFT JOIN finance.legacy_payment_dispositions d
          ON d.legacy_payment_id=p.id
         AND d.organization_id=g.org_id
        WHERE g.org_id =
            NULLIF(current_setting('app.current_org_id', true), '')::uuid;
        """
    )

    for table_name in PAY15_TABLES:
        _enable_rls(table_name)


def downgrade() -> None:
    for table_name in PAY15_TABLES:
        op.execute(
            f"ALTER TABLE finance.{table_name} NO FORCE ROW LEVEL SECURITY;"
        )

    op.execute(
        """
        DO $pay15_guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM finance.legacy_retirement_batches
            )
               OR EXISTS (
                SELECT 1 FROM finance.legacy_invoice_dispositions
            )
               OR EXISTS (
                SELECT 1 FROM finance.legacy_payment_dispositions
            )
               OR EXISTS (
                SELECT 1 FROM finance.legacy_subscription_financial_links
            )
               OR EXISTS (
                SELECT 1 FROM finance.legacy_retirement_audit
            ) THEN
                RAISE EXCEPTION
                    'PAY-15 downgrade blocked: legacy retirement/migration history exists';
            END IF;
        END
        $pay15_guard$;
        """
    )

    op.execute("DROP VIEW IF EXISTS finance.legacy_payments_compat_v;")
    op.execute("DROP VIEW IF EXISTS finance.legacy_invoices_compat_v;")

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_enter_rollback_hold(uuid,text,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_activate_cutover(uuid,text,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_certify_ready(uuid,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_assert_reconciled(uuid);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_record_subscription_link(uuid,uuid,text,uuid,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_record_payment_disposition(uuid,uuid,text,uuid,text,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_record_invoice_disposition(uuid,uuid,text,uuid,text,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_begin_reconciliation(uuid);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_capture_inventory(uuid,text,text,text);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS app_secure.pay15_source_snapshot_sha(uuid);"
    )
    op.execute("RESET ROLE")

    for table_name in PAY15_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{table_name} "
            f"ON finance.{table_name};"
        )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_validate_subscription_link
        ON finance.legacy_subscription_financial_links;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_validate_subscription_link();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_validate_payment_disposition
        ON finance.legacy_payment_dispositions;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_validate_payment_disposition();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_validate_invoice_disposition
        ON finance.legacy_invoice_dispositions;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_validate_invoice_disposition();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_legacy_retirement_batch_touch
        ON finance.legacy_retirement_batches;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_legacy_retirement_batch_lifecycle
        ON finance.legacy_retirement_batches;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_validate_batch_lifecycle();"
    )

    for table_name in (
        "legacy_invoice_dispositions",
        "legacy_payment_dispositions",
        "legacy_subscription_financial_links",
        "legacy_retirement_audit",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_pay15_{table_name}_immutable "
            f"ON finance.{table_name};"
        )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_protect_immutable_history();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_block_legacy_payments_truncate
        ON public.payments;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_block_legacy_invoices_truncate
        ON public.invoices;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_block_legacy_payments_write
        ON public.payments;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_pay15_block_legacy_invoices_write
        ON public.invoices;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS finance.pay15_block_legacy_monetary_write();"
    )

    op.execute(
        """
        REVOKE SELECT (
            id, gym_id, subscription_id, amount, discount_amount,
            status, transaction_reference, razorpay_id
        ) ON TABLE public.payments FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE SELECT (
            id, gym_id, invoice_number, payment_id, subscription_id,
            subtotal, discount_amount, tax_amount, total_amount, status
        ) ON TABLE public.invoices FROM app_security_owner
        """
    )
    op.execute(
        """
        REVOKE SELECT (id, org_id)
        ON TABLE public.subscription_terms FROM app_security_owner
        """
    )

    op.execute("DROP TABLE IF EXISTS finance.legacy_retirement_audit;")
    op.execute(
        "DROP TABLE IF EXISTS finance.legacy_subscription_financial_links;"
    )
    op.execute("DROP TABLE IF EXISTS finance.legacy_payment_dispositions;")
    op.execute("DROP TABLE IF EXISTS finance.legacy_invoice_dispositions;")
    op.execute("DROP TABLE IF EXISTS finance.legacy_retirement_batches;")
