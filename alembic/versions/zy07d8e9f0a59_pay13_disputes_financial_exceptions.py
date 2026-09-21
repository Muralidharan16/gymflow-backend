"""PAY-13 disputes, chargebacks and financial exceptions

Revision ID: zy07d8e9f0a59
Revises: zx07d8e9f0a58
Create Date: 2026-09-21 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "zy07d8e9f0a59"
down_revision: Union[str, Sequence[str], None] = "zx07d8e9f0a58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES = (
    "platform_disputes",
    "platform_dispute_evidence",
    "platform_dispute_events",
    "platform_dispute_financial_entries",
    "platform_financial_exception_cases",
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
        CREATE TABLE public.platform_disputes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            payment_attempt_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            provider_release_id UUID NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            external_dispute_ref VARCHAR(200) NOT NULL,
            dispute_type VARCHAR(80) NOT NULL,
            status TEXT NOT NULL DEFAULT 'opened',
            amount_minor BIGINT NOT NULL,
            currency_code CHAR(3) NOT NULL,
            reason_code VARCHAR(100) NULL,
            financial_hold_active BOOLEAN NOT NULL DEFAULT TRUE,
            provider_decision_ref VARCHAR(200) NULL,
            last_evidence_sha256 CHAR(64) NOT NULL,
            last_evidence_ref VARCHAR(300) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            evidence_due_at TIMESTAMPTZ NULL,
            submitted_at TIMESTAMPTZ NULL,
            decided_at TIMESTAMPTZ NULL,
            hold_released_at TIMESTAMPTZ NULL,
            closed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_disputes_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_disputes_payment_attempt_org
                FOREIGN KEY (payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_disputes_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_disputes_provider_release
                FOREIGN KEY (provider_release_id)
                REFERENCES public.platform_provider_releases(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_disputes_id_org UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_disputes_provider_ref
                UNIQUE (provider_code, environment, external_dispute_ref),
            CONSTRAINT chk_platform_disputes_provider_code
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_disputes_environment
                CHECK (environment IN ('test','live')),
            CONSTRAINT chk_platform_disputes_type
                CHECK (
                    dispute_type IN (
                        'chargeback','cardholder_dispute',
                        'duplicate_charge_allegation','fraud_review'
                    )
                ),
            CONSTRAINT chk_platform_disputes_status
                CHECK (
                    status IN (
                        'opened','evidence_required','submitted',
                        'under_review','won','lost','closed'
                    )
                ),
            CONSTRAINT chk_platform_disputes_amount CHECK (amount_minor > 0),
            CONSTRAINT chk_platform_disputes_currency
                CHECK (
                    currency_code = upper(currency_code)
                    AND currency_code ~ '^[A-Z]{3}$'
                ),
            CONSTRAINT chk_platform_disputes_evidence_sha
                CHECK (last_evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_disputes_evidence_ref
                CHECK (btrim(last_evidence_ref) <> ''),
            CONSTRAINT chk_platform_disputes_hold_shape
                CHECK (
                    (financial_hold_active AND hold_released_at IS NULL)
                    OR
                    (
                        financial_hold_active IS FALSE
                        AND hold_released_at IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_disputes_decision_shape
                CHECK (
                    status NOT IN ('won','lost')
                    OR (
                        provider_decision_ref IS NOT NULL
                        AND decided_at IS NOT NULL
                        AND financial_hold_active IS FALSE
                    )
                ),
            CONSTRAINT chk_platform_disputes_closed_shape
                CHECK (status <> 'closed' OR closed_at IS NOT NULL),
            CONSTRAINT chk_platform_disputes_version CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_disputes_org_status "
        "ON public.platform_disputes (organization_id, status);"
    )
    op.execute(
        "CREATE INDEX ix_platform_disputes_payment "
        "ON public.platform_disputes (organization_id, payment_attempt_id);"
    )
    op.execute(
        "CREATE INDEX ix_platform_disputes_invoice "
        "ON public.platform_disputes (organization_id, invoice_id);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_dispute_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            dispute_id UUID NOT NULL,
            evidence_kind VARCHAR(80) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            source_type VARCHAR(40) NOT NULL,
            source_id UUID NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            observed_at TIMESTAMPTZ NOT NULL,
            submitted_at TIMESTAMPTZ NULL,
            provider_ack_ref VARCHAR(200) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_dispute_evidence_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dispute_evidence_dispute_org
                FOREIGN KEY (dispute_id, organization_id)
                REFERENCES public.platform_disputes(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_dispute_evidence_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_dispute_evidence_sha
                UNIQUE (dispute_id, evidence_sha256),
            CONSTRAINT chk_platform_dispute_evidence_kind
                CHECK (
                    evidence_kind IN (
                        'provider_notice','customer_statement','invoice',
                        'payment_receipt','service_delivery','fraud_signal',
                        'provider_decision','chargeback_reversal'
                    )
                ),
            CONSTRAINT chk_platform_dispute_evidence_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dispute_evidence_ref
                CHECK (btrim(evidence_ref) <> ''),
            CONSTRAINT chk_platform_dispute_evidence_source
                CHECK (
                    source_type IN (
                        'webhook','reconciliation','manual_review','system'
                    )
                ),
            CONSTRAINT chk_platform_dispute_evidence_metadata
                CHECK (jsonb_typeof(metadata_json) = 'object')
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_dispute_evidence_dispute "
        "ON public.platform_dispute_evidence (dispute_id, created_at);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_dispute_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            dispute_id UUID NOT NULL,
            sequence_number BIGINT NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            from_status VARCHAR(40) NULL,
            to_status VARCHAR(40) NULL,
            source_type VARCHAR(40) NOT NULL,
            source_id UUID NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            payload_sha256 CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_dispute_events_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dispute_events_dispute_org
                FOREIGN KEY (dispute_id, organization_id)
                REFERENCES public.platform_disputes(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_dispute_events_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_dispute_events_sequence
                UNIQUE (dispute_id, sequence_number),
            CONSTRAINT uq_platform_dispute_events_evidence
                UNIQUE (dispute_id, evidence_sha256, event_type),
            CONSTRAINT chk_platform_dispute_events_sequence
                CHECK (sequence_number > 0),
            CONSTRAINT chk_platform_dispute_events_source
                CHECK (
                    source_type IN (
                        'webhook','reconciliation','manual_review','system'
                    )
                ),
            CONSTRAINT chk_platform_dispute_events_evidence_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dispute_events_payload_sha
                CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dispute_events_payload
                CHECK (jsonb_typeof(payload_json) = 'object')
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_dispute_events_dispute "
        "ON public.platform_dispute_events (dispute_id, sequence_number);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_dispute_financial_entries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            dispute_id UUID NOT NULL,
            payment_attempt_id UUID NOT NULL,
            entry_type VARCHAR(80) NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency_code CHAR(3) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_dispute_financial_entries_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dispute_financial_entries_dispute_org
                FOREIGN KEY (dispute_id, organization_id)
                REFERENCES public.platform_disputes(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dispute_financial_entries_payment_org
                FOREIGN KEY (payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_dispute_financial_entries_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_dispute_financial_entries_type
                UNIQUE (dispute_id, entry_type),
            CONSTRAINT chk_platform_dispute_financial_entries_type
                CHECK (
                    entry_type IN (
                        'liability_recognized','liability_reversed',
                        'loss_recognized','loss_reversed'
                    )
                ),
            CONSTRAINT chk_platform_dispute_financial_entries_amount
                CHECK (amount_minor > 0),
            CONSTRAINT chk_platform_dispute_financial_entries_currency
                CHECK (
                    currency_code = upper(currency_code)
                    AND currency_code ~ '^[A-Z]{3}$'
                ),
            CONSTRAINT chk_platform_dispute_financial_entries_evidence_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dispute_financial_entries_evidence_ref
                CHECK (btrim(evidence_ref) <> '')
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_dispute_financial_entries_dispute "
        "ON public.platform_dispute_financial_entries (dispute_id, effective_at);"
    )

    op.execute(
        """
        CREATE TABLE public.platform_financial_exception_cases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL,
            payment_attempt_id UUID NULL,
            invoice_id UUID NULL,
            refund_id UUID NULL,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            exception_type VARCHAR(100) NOT NULL,
            status TEXT NOT NULL DEFAULT 'detected',
            severity VARCHAR(20) NOT NULL DEFAULT 'warning',
            external_object_type VARCHAR(80) NOT NULL,
            external_object_ref VARCHAR(200) NOT NULL,
            amount_minor BIGINT NULL,
            currency_code CHAR(3) NULL,
            initial_evidence_sha256 CHAR(64) NOT NULL,
            initial_evidence_ref VARCHAR(300) NOT NULL,
            source_type VARCHAR(40) NOT NULL,
            source_id UUID NULL,
            manual_review_required BOOLEAN NOT NULL DEFAULT TRUE,
            automatic_financial_mutation_allowed BOOLEAN NOT NULL DEFAULT FALSE,
            resolution_code VARCHAR(100) NULL,
            resolution_detail_safe TEXT NULL,
            detected_at TIMESTAMPTZ NOT NULL,
            mapped_at TIMESTAMPTZ NULL,
            resolved_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_financial_exception_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_financial_exception_payment_org
                FOREIGN KEY (payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_financial_exception_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_financial_exception_refund_org
                FOREIGN KEY (refund_id, organization_id)
                REFERENCES public.platform_refunds(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_financial_exception_fact
                UNIQUE (
                    provider_code, environment, exception_type,
                    external_object_ref, initial_evidence_sha256
                ),
            CONSTRAINT chk_platform_financial_exception_provider_code
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_financial_exception_environment
                CHECK (environment IN ('test','live')),
            CONSTRAINT chk_platform_financial_exception_type
                CHECK (
                    exception_type IN (
                        'accidental_duplicate_provider_payment',
                        'orphan_provider_payment','orphan_settlement',
                        'unknown_refund','wrong_customer_mapping',
                        'unmapped_dispute'
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_status
                CHECK (
                    status IN (
                        'detected','quarantined','investigating',
                        'mapped','resolved','ignored'
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_severity
                CHECK (severity IN ('info','warning','critical')),
            CONSTRAINT chk_platform_financial_exception_amount
                CHECK (amount_minor IS NULL OR amount_minor > 0),
            CONSTRAINT chk_platform_financial_exception_currency
                CHECK (
                    currency_code IS NULL
                    OR (
                        currency_code = upper(currency_code)
                        AND currency_code ~ '^[A-Z]{3}$'
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_evidence_sha
                CHECK (initial_evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_financial_exception_evidence_ref
                CHECK (btrim(initial_evidence_ref) <> ''),
            CONSTRAINT chk_platform_financial_exception_source
                CHECK (
                    source_type IN (
                        'webhook','reconciliation','manual_review','system'
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_manual_review
                CHECK (manual_review_required IS TRUE),
            CONSTRAINT chk_platform_financial_exception_no_auto_mutation
                CHECK (automatic_financial_mutation_allowed IS FALSE),
            CONSTRAINT chk_platform_financial_exception_mapped_shape
                CHECK (
                    status <> 'mapped'
                    OR (
                        organization_id IS NOT NULL
                        AND mapped_at IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_resolution_shape
                CHECK (
                    status NOT IN ('resolved','ignored')
                    OR (
                        resolution_code IS NOT NULL
                        AND resolved_at IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_financial_exception_version
                CHECK (version >= 1)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_platform_financial_exception_status "
        "ON public.platform_financial_exception_cases "
        "(status, severity, detected_at);"
    )
    op.execute(
        "CREATE INDEX ix_platform_financial_exception_org "
        "ON public.platform_financial_exception_cases "
        "(organization_id, status);"
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_validate_dispute()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            payment_status TEXT;
            payment_invoice UUID;
            payment_provider VARCHAR(40);
            payment_release UUID;
            payment_amount BIGINT;
            payment_currency CHAR(3);
            evidence_count BIGINT;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT
                    status, invoice_id, provider_code, provider_release_id,
                    amount_minor, currency_code
                INTO
                    payment_status, payment_invoice, payment_provider,
                    payment_release, payment_amount, payment_currency
                FROM public.platform_payment_attempts
                WHERE id=NEW.payment_attempt_id
                  AND organization_id=NEW.organization_id;

                IF payment_status IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-13 dispute requires mapped payment attempt';
                END IF;
                IF payment_status <> 'succeeded' THEN
                    RAISE EXCEPTION
                        'PAY-13 dispute requires historically captured payment';
                END IF;
                IF payment_invoice IS DISTINCT FROM NEW.invoice_id
                   OR payment_provider IS DISTINCT FROM NEW.provider_code
                   OR payment_release IS DISTINCT FROM NEW.provider_release_id
                   OR payment_currency IS DISTINCT FROM NEW.currency_code
                   OR NEW.amount_minor > payment_amount THEN
                    RAISE EXCEPTION
                        'PAY-13 dispute must exactly bind provider/payment truth';
                END IF;
                IF NEW.status <> 'opened'
                   OR NEW.financial_hold_active IS FALSE
                   OR NEW.hold_released_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-13 disputes must open with financial hold active';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.payment_attempt_id, NEW.invoice_id,
                NEW.provider_release_id, NEW.provider_code, NEW.environment,
                NEW.external_dispute_ref, NEW.dispute_type, NEW.amount_minor,
                NEW.currency_code, NEW.opened_at, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.payment_attempt_id, OLD.invoice_id,
                OLD.provider_release_id, OLD.provider_code, OLD.environment,
                OLD.external_dispute_ref, OLD.dispute_type, OLD.amount_minor,
                OLD.currency_code, OLD.opened_at, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'PAY-13 dispute identity is immutable';
            END IF;

            IF OLD.status='closed'
               AND ROW(
                    NEW.status, NEW.provider_decision_ref,
                    NEW.financial_hold_active, NEW.closed_at
               ) IS DISTINCT FROM ROW(
                    OLD.status, OLD.provider_decision_ref,
                    OLD.financial_hold_active, OLD.closed_at
               ) THEN
                RAISE EXCEPTION 'PAY-13 closed dispute is terminal';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='opened' AND NEW.status IN (
                        'evidence_required','submitted','under_review','won','lost'
                    ))
                    OR
                    (OLD.status='evidence_required' AND NEW.status IN (
                        'submitted','closed'
                    ))
                    OR
                    (OLD.status='submitted' AND NEW.status IN (
                        'under_review','won','lost'
                    ))
                    OR
                    (OLD.status='under_review' AND NEW.status IN ('won','lost'))
                    OR
                    (OLD.status IN ('won','lost') AND NEW.status='closed')
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 forbidden dispute transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status IN ('opened','evidence_required','submitted','under_review')
               AND (
                    NEW.financial_hold_active IS FALSE
                    OR NEW.hold_released_at IS NOT NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 unresolved dispute financial hold cannot be released';
            END IF;

            IF NEW.status='submitted' THEN
                SELECT count(*) INTO evidence_count
                FROM public.platform_dispute_evidence
                WHERE dispute_id=NEW.id
                  AND organization_id=NEW.organization_id;
                IF evidence_count=0 OR NEW.submitted_at IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-13 submitted dispute requires durable evidence';
                END IF;
            END IF;

            IF NEW.status IN ('won','lost')
               AND (
                    NEW.provider_decision_ref IS NULL
                    OR NEW.decided_at IS NULL
                    OR NEW.financial_hold_active
                    OR NEW.hold_released_at IS NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 provider decision requires decision evidence and hold release';
            END IF;

            IF NEW.status='closed'
               AND (
                    OLD.status NOT IN ('won','lost','evidence_required')
                    OR NEW.closed_at IS NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 close requires resolved/withdrawn dispute state';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_disputes_pay13_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_disputes
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_validate_dispute();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_protect_dispute_evidence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'PAY-13 dispute evidence is append-only';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_dispute_evidence_append_only
        BEFORE UPDATE OR DELETE ON public.platform_dispute_evidence
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_protect_dispute_evidence();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_validate_dispute_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_sequence BIGINT;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'PAY-13 dispute events are append-only';
            END IF;

            SELECT COALESCE(max(sequence_number), 0) + 1
            INTO expected_sequence
            FROM public.platform_dispute_events
            WHERE dispute_id=NEW.dispute_id;

            IF NEW.sequence_number <> expected_sequence THEN
                RAISE EXCEPTION
                    'PAY-13 dispute event sequence must be contiguous: expected %, got %',
                    expected_sequence, NEW.sequence_number;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_dispute_events_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_dispute_events
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_validate_dispute_event();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_validate_dispute_financial_entry()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            dispute_status TEXT;
            dispute_payment UUID;
            dispute_amount BIGINT;
            dispute_currency CHAR(3);
            loss_count BIGINT;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION
                    'PAY-13 dispute financial entries are append-only';
            END IF;

            SELECT status, payment_attempt_id, amount_minor, currency_code
            INTO dispute_status, dispute_payment, dispute_amount, dispute_currency
            FROM public.platform_disputes
            WHERE id=NEW.dispute_id
              AND organization_id=NEW.organization_id;

            IF dispute_status IS NULL
               OR dispute_payment IS DISTINCT FROM NEW.payment_attempt_id
               OR dispute_amount IS DISTINCT FROM NEW.amount_minor
               OR dispute_currency IS DISTINCT FROM NEW.currency_code THEN
                RAISE EXCEPTION
                    'PAY-13 financial entry must exactly bind dispute amount/payment';
            END IF;

            IF NEW.entry_type='liability_recognized'
               AND dispute_status NOT IN (
                    'opened','evidence_required','submitted','under_review'
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 liability recognition requires unresolved dispute';
            END IF;

            IF NEW.entry_type='liability_reversed'
               AND dispute_status NOT IN ('won','closed') THEN
                RAISE EXCEPTION
                    'PAY-13 liability reversal requires won dispute';
            END IF;

            IF NEW.entry_type='loss_recognized'
               AND dispute_status NOT IN ('lost','closed') THEN
                RAISE EXCEPTION
                    'PAY-13 loss recognition requires lost dispute';
            END IF;

            IF NEW.entry_type='loss_reversed' THEN
                SELECT count(*) INTO loss_count
                FROM public.platform_dispute_financial_entries
                WHERE dispute_id=NEW.dispute_id
                  AND entry_type='loss_recognized';
                IF dispute_status NOT IN ('lost','closed') OR loss_count <> 1 THEN
                    RAISE EXCEPTION
                        'PAY-13 chargeback reversal requires recorded dispute loss';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_dispute_financial_entries_append_only
        BEFORE INSERT OR UPDATE OR DELETE
        ON public.platform_dispute_financial_entries
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_validate_dispute_financial_entry();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_require_dispute_financial_closure()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            matching_entries BIGINT;
        BEGIN
            IF TG_OP='INSERT' THEN
                SELECT count(*) INTO matching_entries
                FROM public.platform_dispute_financial_entries
                WHERE dispute_id=NEW.id
                  AND organization_id=NEW.organization_id
                  AND entry_type='liability_recognized';
                IF matching_entries <> 1 THEN
                    RAISE EXCEPTION
                        'PAY-13 opened dispute requires liability entry in same transaction';
                END IF;
                RETURN NULL;
            END IF;

            IF NEW.status='won' AND OLD.status IS DISTINCT FROM NEW.status THEN
                SELECT count(*) INTO matching_entries
                FROM public.platform_dispute_financial_entries
                WHERE dispute_id=NEW.id
                  AND organization_id=NEW.organization_id
                  AND entry_type='liability_reversed';
                IF matching_entries <> 1 THEN
                    RAISE EXCEPTION
                        'PAY-13 won dispute requires liability reversal in same transaction';
                END IF;
            ELSIF NEW.status='lost' AND OLD.status IS DISTINCT FROM NEW.status THEN
                SELECT count(*) INTO matching_entries
                FROM public.platform_dispute_financial_entries
                WHERE dispute_id=NEW.id
                  AND organization_id=NEW.organization_id
                  AND entry_type='loss_recognized';
                IF matching_entries <> 1 THEN
                    RAISE EXCEPTION
                        'PAY-13 lost dispute requires loss recognition in same transaction';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_platform_disputes_pay13_financial_closure
        AFTER INSERT OR UPDATE OF status ON public.platform_disputes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_require_dispute_financial_closure();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_validate_financial_exception()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.status NOT IN ('detected','quarantined')
                   OR NEW.manual_review_required IS FALSE
                   OR NEW.automatic_financial_mutation_allowed THEN
                    RAISE EXCEPTION
                        'PAY-13 exception intake must fail closed into manual review';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.provider_code, NEW.environment, NEW.exception_type,
                NEW.external_object_type, NEW.external_object_ref,
                NEW.initial_evidence_sha256, NEW.initial_evidence_ref,
                NEW.source_type, NEW.source_id, NEW.detected_at, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.provider_code, OLD.environment, OLD.exception_type,
                OLD.external_object_type, OLD.external_object_ref,
                OLD.initial_evidence_sha256, OLD.initial_evidence_ref,
                OLD.source_type, OLD.source_id, OLD.detected_at, OLD.created_at
            ) THEN
                RAISE EXCEPTION
                    'PAY-13 financial exception source identity is immutable';
            END IF;

            IF OLD.status IN ('resolved','ignored')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-13 resolved financial exception is terminal';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='detected' AND NEW.status IN (
                        'quarantined','investigating','mapped','resolved','ignored'
                    ))
                    OR
                    (OLD.status='quarantined' AND NEW.status IN (
                        'investigating','mapped','resolved','ignored'
                    ))
                    OR
                    (OLD.status='investigating' AND NEW.status IN (
                        'mapped','resolved','ignored'
                    ))
                    OR
                    (OLD.status='mapped' AND NEW.status IN ('resolved','ignored'))
               ) THEN
                RAISE EXCEPTION
                    'PAY-13 forbidden financial exception transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.manual_review_required IS FALSE
               OR NEW.automatic_financial_mutation_allowed THEN
                RAISE EXCEPTION
                    'PAY-13 financial exceptions cannot enable automatic money mutation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_financial_exception_pay13_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_financial_exception_cases
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_validate_financial_exception();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_guard_payment_attempt_under_dispute()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            hold_count BIGINT;
            dispute_count BIGINT;
        BEGIN
            IF TG_OP='INSERT' THEN
                SELECT count(*) INTO hold_count
                FROM public.platform_disputes
                WHERE organization_id=NEW.organization_id
                  AND invoice_id=NEW.invoice_id
                  AND financial_hold_active;
                IF hold_count > 0 THEN
                    RAISE EXCEPTION
                        'PAY-13 active dispute freezes new payment attempts on disputed invoice';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status='succeeded'
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                SELECT count(*) INTO dispute_count
                FROM public.platform_disputes
                WHERE organization_id=OLD.organization_id
                  AND payment_attempt_id=OLD.id;
                IF dispute_count > 0 THEN
                    RAISE EXCEPTION
                        'PAY-13 captured payment truth is immutable after dispute';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_payment_attempts_pay13_dispute_guard
        BEFORE INSERT OR UPDATE OF status ON public.platform_payment_attempts
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_guard_payment_attempt_under_dispute();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay13_guard_refund_under_dispute()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            hold_count BIGINT;
        BEGIN
            SELECT count(*) INTO hold_count
            FROM public.platform_disputes
            WHERE organization_id=NEW.organization_id
              AND (
                    payment_attempt_id=NEW.payment_attempt_id
                    OR invoice_id=NEW.invoice_id
              )
              AND financial_hold_active;

            IF hold_count=0 THEN
                RETURN NEW;
            END IF;

            IF TG_OP='INSERT' THEN
                RAISE EXCEPTION
                    'PAY-13 active dispute freezes new refund requests';
            END IF;

            IF ROW(
                NEW.organization_id, NEW.payment_attempt_id,
                NEW.invoice_id, NEW.amount_minor, NEW.currency_code
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.payment_attempt_id,
                OLD.invoice_id, OLD.amount_minor, OLD.currency_code
            ) THEN
                RAISE EXCEPTION
                    'PAY-13 active dispute freezes refund financial identity';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NEW.status IN ('approved','provider_pending') THEN
                RAISE EXCEPTION
                    'PAY-13 active dispute freezes refund execution';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_refunds_pay13_dispute_guard
        BEFORE INSERT OR UPDATE ON public.platform_refunds
        FOR EACH ROW
        EXECUTE FUNCTION public.pay13_guard_refund_under_dispute();
        """
    )

    for table_name in (
        "platform_disputes",
        "platform_financial_exception_cases",
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
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_runtime') THEN
                GRANT SELECT ON
                    public.platform_disputes,
                    public.platform_dispute_evidence,
                    public.platform_dispute_events,
                    public.platform_dispute_financial_entries,
                    public.platform_financial_exception_cases
                TO app_runtime;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table_name in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY;"
        )

    op.execute(
        """
        DO $pay13_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.platform_disputes)
               OR EXISTS (SELECT 1 FROM public.platform_dispute_evidence)
               OR EXISTS (SELECT 1 FROM public.platform_dispute_events)
               OR EXISTS (SELECT 1 FROM public.platform_dispute_financial_entries)
               OR EXISTS (SELECT 1 FROM public.platform_financial_exception_cases) THEN
                RAISE EXCEPTION
                    'PAY-13 downgrade blocked: dispute/exception financial history exists';
            END IF;
        END
        $pay13_guard$;
        """
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_refunds_pay13_dispute_guard
        ON public.platform_refunds;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_guard_refund_under_dispute();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_payment_attempts_pay13_dispute_guard
        ON public.platform_payment_attempts;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_guard_payment_attempt_under_dispute();"
    )

    for table_name in TENANT_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{table_name} "
            f"ON public.{table_name};"
        )

    for table_name in (
        "platform_financial_exception_cases",
        "platform_disputes",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at "
            f"ON public.{table_name};"
        )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_financial_exception_pay13_lifecycle
        ON public.platform_financial_exception_cases;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_validate_financial_exception();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_disputes_pay13_financial_closure
        ON public.platform_disputes;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_require_dispute_financial_closure();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS
            trg_platform_dispute_financial_entries_append_only
        ON public.platform_dispute_financial_entries;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_validate_dispute_financial_entry();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_dispute_events_append_only
        ON public.platform_dispute_events;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_validate_dispute_event();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_dispute_evidence_append_only
        ON public.platform_dispute_evidence;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_protect_dispute_evidence();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_disputes_pay13_lifecycle
        ON public.platform_disputes;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay13_validate_dispute();"
    )

    op.execute("DROP TABLE IF EXISTS public.platform_financial_exception_cases;")
    op.execute("DROP TABLE IF EXISTS public.platform_dispute_financial_entries;")
    op.execute("DROP TABLE IF EXISTS public.platform_dispute_events;")
    op.execute("DROP TABLE IF EXISTS public.platform_dispute_evidence;")
    op.execute("DROP TABLE IF EXISTS public.platform_disputes;")
