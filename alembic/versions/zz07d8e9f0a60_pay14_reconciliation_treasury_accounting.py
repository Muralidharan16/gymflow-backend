"""PAY-14 reconciliation, treasury and accounting closure

Revision ID: zz07d8e9f0a60
Revises: zy07d8e9f0a59
Create Date: 2026-09-21 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "zz07d8e9f0a60"
down_revision: Union[str, Sequence[str], None] = "zy07d8e9f0a59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAY14_TABLES = (
    "platform_accounting_closure_runs",
    "platform_accounting_evidence",
    "platform_accounting_reconciliation_items",
    "platform_accounting_incidents",
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
            organization_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::uuid
        )
        WITH CHECK (
            organization_id = NULLIF(
                current_setting('app.current_org_id', true), ''
            )::uuid
        );
        """
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.platform_accounting_closure_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NULL,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            closure_key VARCHAR(180) NOT NULL,
            period_start TIMESTAMPTZ NOT NULL,
            period_end TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'collecting',
            expected_object_count INTEGER NOT NULL DEFAULT 0,
            observed_object_count INTEGER NOT NULL DEFAULT 0,
            mismatch_count INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            manual_review_count INTEGER NOT NULL DEFAULT 0,
            incident_count INTEGER NOT NULL DEFAULT 0,
            resolved_count INTEGER NOT NULL DEFAULT 0,
            evidence_manifest_sha256 CHAR(64) NULL,
            evidence_manifest_ref VARCHAR(300) NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            ready_at TIMESTAMPTZ NULL,
            closed_at TIMESTAMPTZ NULL,
            closed_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_accounting_closure_org
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_accounting_closure_runs_key
                UNIQUE (provider_code, environment, closure_key),
            CONSTRAINT uq_platform_accounting_closure_runs_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_accounting_closure_provider
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_accounting_closure_environment
                CHECK (environment IN ('test','live')),
            CONSTRAINT chk_platform_accounting_closure_key
                CHECK (btrim(closure_key) <> ''),
            CONSTRAINT chk_platform_accounting_closure_period
                CHECK (period_end > period_start),
            CONSTRAINT chk_platform_accounting_closure_status
                CHECK (
                    status IN (
                        'collecting','reconciling','review_required',
                        'ready_to_close','closed','failed'
                    )
                ),
            CONSTRAINT chk_platform_accounting_closure_counts
                CHECK (
                    expected_object_count >= 0
                    AND observed_object_count >= 0
                    AND mismatch_count >= 0
                    AND retry_count >= 0
                    AND manual_review_count >= 0
                    AND incident_count >= 0
                    AND resolved_count >= 0
                ),
            CONSTRAINT chk_platform_accounting_closure_manifest_sha
                CHECK (
                    evidence_manifest_sha256 IS NULL
                    OR evidence_manifest_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_accounting_closure_manifest_pair
                CHECK (
                    (
                        evidence_manifest_sha256 IS NULL
                        AND evidence_manifest_ref IS NULL
                    )
                    OR (
                        evidence_manifest_sha256 IS NOT NULL
                        AND evidence_manifest_ref IS NOT NULL
                        AND btrim(evidence_manifest_ref) <> ''
                    )
                ),
            CONSTRAINT chk_platform_accounting_closure_ready_shape
                CHECK (status <> 'ready_to_close' OR ready_at IS NOT NULL),
            CONSTRAINT chk_platform_accounting_closure_closed_shape
                CHECK (
                    status <> 'closed'
                    OR (
                        ready_at IS NOT NULL
                        AND closed_at IS NOT NULL
                        AND closed_by IS NOT NULL
                        AND evidence_manifest_sha256 IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_accounting_closure_version
                CHECK (version >= 1)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_closure_status
        ON public.platform_accounting_closure_runs
            (provider_code, status, period_end);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_closure_org
        ON public.platform_accounting_closure_runs
            (organization_id, period_end);
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_accounting_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            closure_run_id UUID NOT NULL,
            organization_id UUID NULL,
            side VARCHAR(20) NOT NULL,
            object_type VARCHAR(40) NOT NULL,
            local_object_id UUID NULL,
            object_ref VARCHAR(220) NOT NULL,
            provider_code VARCHAR(40) NOT NULL,
            environment VARCHAR(20) NOT NULL,
            amount_minor BIGINT NULL,
            fee_minor BIGINT NULL,
            currency_code CHAR(3) NULL,
            object_status VARCHAR(80) NULL,
            evidence_kind VARCHAR(60) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            authoritative BOOLEAN NOT NULL DEFAULT FALSE,
            observed_at TIMESTAMPTZ NOT NULL,
            safe_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_accounting_evidence_run
                FOREIGN KEY (closure_run_id)
                REFERENCES public.platform_accounting_closure_runs(id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_evidence_org
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_accounting_evidence_fact
                UNIQUE (
                    closure_run_id, side, object_type,
                    object_ref, evidence_sha256
                ),
            CONSTRAINT uq_platform_accounting_evidence_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_accounting_evidence_side
                CHECK (side IN ('local','provider','settlement')),
            CONSTRAINT chk_platform_accounting_evidence_object_type
                CHECK (
                    object_type IN (
                        'captured_payment','settlement','gateway_fee',
                        'refund','refund_fee','dispute','chargeback',
                        'adjustment'
                    )
                ),
            CONSTRAINT chk_platform_accounting_evidence_object_ref
                CHECK (btrim(object_ref) <> ''),
            CONSTRAINT chk_platform_accounting_evidence_provider
                CHECK (provider_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_accounting_evidence_environment
                CHECK (environment IN ('test','live')),
            CONSTRAINT chk_platform_accounting_evidence_amount
                CHECK (amount_minor IS NULL OR amount_minor >= 0),
            CONSTRAINT chk_platform_accounting_evidence_fee
                CHECK (fee_minor IS NULL OR fee_minor >= 0),
            CONSTRAINT chk_platform_accounting_evidence_currency
                CHECK (
                    currency_code IS NULL
                    OR (
                        currency_code = upper(currency_code)
                        AND currency_code ~ '^[A-Z]{3}$'
                    )
                ),
            CONSTRAINT chk_platform_accounting_evidence_kind
                CHECK (
                    evidence_kind IN (
                        'local_snapshot','provider_api','provider_statement',
                        'provider_settlement','bank_statement',
                        'manual_attestation'
                    )
                ),
            CONSTRAINT chk_platform_accounting_evidence_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_accounting_evidence_ref
                CHECK (btrim(evidence_ref) <> ''),
            CONSTRAINT chk_platform_accounting_evidence_metadata
                CHECK (jsonb_typeof(safe_metadata_json) = 'object')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_evidence_run
        ON public.platform_accounting_evidence
            (closure_run_id, object_type, object_ref);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_evidence_org
        ON public.platform_accounting_evidence
            (organization_id, object_type);
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_accounting_reconciliation_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            closure_run_id UUID NOT NULL,
            organization_id UUID NULL,
            object_type VARCHAR(40) NOT NULL,
            reconciliation_key VARCHAR(320) NOT NULL,
            local_object_id UUID NULL,
            local_evidence_id UUID NULL,
            provider_evidence_id UUID NULL,
            settlement_evidence_id UUID NULL,
            mismatch_category VARCHAR(60) NULL,
            safe_outcome VARCHAR(80) NOT NULL,
            resolution_status TEXT NOT NULL DEFAULT 'open',
            authoritative_evidence_complete BOOLEAN NOT NULL DEFAULT FALSE,
            automatic_financial_mutation_allowed BOOLEAN NOT NULL DEFAULT FALSE,
            reason_code VARCHAR(100) NOT NULL,
            resolution_evidence_sha256 CHAR(64) NULL,
            resolution_evidence_ref VARCHAR(300) NULL,
            first_detected_at TIMESTAMPTZ NOT NULL,
            last_checked_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_accounting_item_run
                FOREIGN KEY (closure_run_id)
                REFERENCES public.platform_accounting_closure_runs(id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_item_org
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_item_local_evidence_org
                FOREIGN KEY (local_evidence_id, organization_id)
                REFERENCES public.platform_accounting_evidence(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_item_provider_evidence_org
                FOREIGN KEY (provider_evidence_id, organization_id)
                REFERENCES public.platform_accounting_evidence(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_item_settlement_evidence_org
                FOREIGN KEY (settlement_evidence_id, organization_id)
                REFERENCES public.platform_accounting_evidence(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_accounting_reconciliation_item_key
                UNIQUE (closure_run_id, reconciliation_key),
            CONSTRAINT uq_platform_accounting_reconciliation_item_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_accounting_item_object_type
                CHECK (
                    object_type IN (
                        'captured_payment','settlement','gateway_fee',
                        'refund','refund_fee','dispute','chargeback',
                        'adjustment'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_key
                CHECK (btrim(reconciliation_key) <> ''),
            CONSTRAINT chk_platform_accounting_item_mismatch
                CHECK (
                    mismatch_category IS NULL
                    OR mismatch_category IN (
                        'provider_only','local_only','amount_mismatch',
                        'currency_mismatch','status_mismatch',
                        'settlement_missing','duplicate_provider_object',
                        'unknown_provider_object','refund_mismatch',
                        'fee_mismatch'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_outcome
                CHECK (
                    safe_outcome IN (
                        'auto_resolved_by_authoritative_evidence',
                        'retry_required','manual_review_required',
                        'security_incident','accounting_incident'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_resolution_status
                CHECK (
                    resolution_status IN (
                        'open','retry_pending','under_review',
                        'resolved','incident_open'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_no_auto_money_mutation
                CHECK (automatic_financial_mutation_allowed IS FALSE),
            CONSTRAINT chk_platform_accounting_item_reason
                CHECK (btrim(reason_code) <> ''),
            CONSTRAINT chk_platform_accounting_item_resolution_evidence
                CHECK (
                    (
                        resolution_evidence_sha256 IS NULL
                        AND resolution_evidence_ref IS NULL
                    )
                    OR (
                        resolution_evidence_sha256 ~ '^[0-9a-f]{64}$'
                        AND resolution_evidence_ref IS NOT NULL
                        AND btrim(resolution_evidence_ref) <> ''
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_resolved_shape
                CHECK (
                    resolution_status <> 'resolved'
                    OR (
                        resolved_at IS NOT NULL
                        AND resolution_evidence_sha256 IS NOT NULL
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_auto_resolution_shape
                CHECK (
                    safe_outcome <> 'auto_resolved_by_authoritative_evidence'
                    OR (
                        mismatch_category IS NULL
                        AND authoritative_evidence_complete IS TRUE
                        AND resolution_status='resolved'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_retry_shape
                CHECK (
                    safe_outcome <> 'retry_required'
                    OR resolution_status IN (
                        'open','retry_pending','resolved'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_manual_shape
                CHECK (
                    safe_outcome <> 'manual_review_required'
                    OR resolution_status IN (
                        'open','under_review','resolved'
                    )
                ),
            CONSTRAINT chk_platform_accounting_item_incident_shape
                CHECK (
                    safe_outcome NOT IN (
                        'security_incident','accounting_incident'
                    )
                    OR resolution_status IN ('incident_open','resolved')
                ),
            CONSTRAINT chk_platform_accounting_item_version
                CHECK (version >= 1)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_item_run_status
        ON public.platform_accounting_reconciliation_items
            (closure_run_id, resolution_status);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_item_org
        ON public.platform_accounting_reconciliation_items
            (organization_id, resolution_status);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_item_mismatch
        ON public.platform_accounting_reconciliation_items
            (mismatch_category, safe_outcome);
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_accounting_incidents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            closure_run_id UUID NOT NULL,
            reconciliation_item_id UUID NOT NULL,
            organization_id UUID NULL,
            incident_type VARCHAR(40) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            incident_code VARCHAR(100) NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ NULL,
            resolution_code VARCHAR(100) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_accounting_incident_run
                FOREIGN KEY (closure_run_id)
                REFERENCES public.platform_accounting_closure_runs(id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_incident_org
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_accounting_incident_item_org
                FOREIGN KEY (reconciliation_item_id, organization_id)
                REFERENCES public.platform_accounting_reconciliation_items(
                    id, organization_id
                ) ON DELETE RESTRICT,
            CONSTRAINT uq_platform_accounting_incident_item_type
                UNIQUE (reconciliation_item_id, incident_type),
            CONSTRAINT chk_platform_accounting_incident_type
                CHECK (
                    incident_type IN (
                        'security_incident','accounting_incident'
                    )
                ),
            CONSTRAINT chk_platform_accounting_incident_severity
                CHECK (severity IN ('warning','critical')),
            CONSTRAINT chk_platform_accounting_incident_status
                CHECK (status IN ('open','investigating','resolved')),
            CONSTRAINT chk_platform_accounting_incident_code
                CHECK (btrim(incident_code) <> ''),
            CONSTRAINT chk_platform_accounting_incident_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_accounting_incident_ref
                CHECK (btrim(evidence_ref) <> ''),
            CONSTRAINT chk_platform_accounting_incident_resolved_shape
                CHECK (
                    status <> 'resolved'
                    OR (
                        resolved_at IS NOT NULL
                        AND resolution_code IS NOT NULL
                    )
                )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_incident_status
        ON public.platform_accounting_incidents
            (status, severity, opened_at);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_accounting_incident_org
        ON public.platform_accounting_incidents
            (organization_id, status);
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay14_validate_accounting_evidence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            run_provider VARCHAR(40);
            run_environment VARCHAR(20);
            run_org UUID;
            run_status TEXT;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'PAY-14 accounting evidence is append-only';
            END IF;

            SELECT provider_code, environment, organization_id, status
            INTO run_provider, run_environment, run_org, run_status
            FROM public.platform_accounting_closure_runs
            WHERE id=NEW.closure_run_id;

            IF run_provider IS NULL THEN
                RAISE EXCEPTION 'PAY-14 evidence closure run does not exist';
            END IF;
            IF run_status IN ('closed','failed') THEN
                RAISE EXCEPTION
                    'PAY-14 terminal closure cannot accept new evidence';
            END IF;
            IF NEW.provider_code IS DISTINCT FROM run_provider
               OR NEW.environment IS DISTINCT FROM run_environment
               OR NEW.organization_id IS DISTINCT FROM run_org THEN
                RAISE EXCEPTION
                    'PAY-14 evidence must match closure provider/environment/tenant scope';
            END IF;

            IF NEW.side='local'
               AND NEW.evidence_kind <> 'local_snapshot' THEN
                RAISE EXCEPTION
                    'PAY-14 local evidence must be an immutable local snapshot';
            END IF;
            IF NEW.side='settlement'
               AND NEW.evidence_kind NOT IN (
                    'provider_settlement','bank_statement',
                    'manual_attestation'
               ) THEN
                RAISE EXCEPTION
                    'PAY-14 settlement side requires settlement/bank evidence';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_accounting_evidence_append_only
        BEFORE INSERT OR UPDATE OR DELETE
        ON public.platform_accounting_evidence
        FOR EACH ROW
        EXECUTE FUNCTION public.pay14_validate_accounting_evidence();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay14_validate_reconciliation_item()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            run_status TEXT;
            run_org UUID;
            evidence_run UUID;
            evidence_org UUID;
            evidence_side TEXT;
            evidence_authoritative BOOLEAN;
        BEGIN
            SELECT status, organization_id
            INTO run_status, run_org
            FROM public.platform_accounting_closure_runs
            WHERE id=NEW.closure_run_id;

            IF run_status IS NULL THEN
                RAISE EXCEPTION 'PAY-14 reconciliation closure run does not exist';
            END IF;
            IF run_status IN ('closed','failed') THEN
                RAISE EXCEPTION
                    'PAY-14 terminal closure cannot mutate reconciliation items';
            END IF;
            IF NEW.organization_id IS DISTINCT FROM run_org THEN
                RAISE EXCEPTION
                    'PAY-14 reconciliation item tenant must match closure scope';
            END IF;
            IF NEW.automatic_financial_mutation_allowed THEN
                RAISE EXCEPTION
                    'PAY-14 automated reconciliation may never correct money';
            END IF;

            IF NEW.local_evidence_id IS NOT NULL THEN
                SELECT closure_run_id, organization_id, side, authoritative
                INTO evidence_run, evidence_org, evidence_side, evidence_authoritative
                FROM public.platform_accounting_evidence
                WHERE id=NEW.local_evidence_id;
                IF evidence_run IS DISTINCT FROM NEW.closure_run_id
                   OR evidence_org IS DISTINCT FROM NEW.organization_id
                   OR evidence_side <> 'local' THEN
                    RAISE EXCEPTION
                        'PAY-14 local evidence binding is invalid';
                END IF;
            END IF;

            IF NEW.provider_evidence_id IS NOT NULL THEN
                SELECT closure_run_id, organization_id, side, authoritative
                INTO evidence_run, evidence_org, evidence_side, evidence_authoritative
                FROM public.platform_accounting_evidence
                WHERE id=NEW.provider_evidence_id;
                IF evidence_run IS DISTINCT FROM NEW.closure_run_id
                   OR evidence_org IS DISTINCT FROM NEW.organization_id
                   OR evidence_side <> 'provider' THEN
                    RAISE EXCEPTION
                        'PAY-14 provider evidence binding is invalid';
                END IF;
            END IF;

            IF NEW.settlement_evidence_id IS NOT NULL THEN
                SELECT closure_run_id, organization_id, side, authoritative
                INTO evidence_run, evidence_org, evidence_side, evidence_authoritative
                FROM public.platform_accounting_evidence
                WHERE id=NEW.settlement_evidence_id;
                IF evidence_run IS DISTINCT FROM NEW.closure_run_id
                   OR evidence_org IS DISTINCT FROM NEW.organization_id
                   OR evidence_side <> 'settlement' THEN
                    RAISE EXCEPTION
                        'PAY-14 settlement evidence binding is invalid';
                END IF;
            END IF;

            IF NEW.safe_outcome='auto_resolved_by_authoritative_evidence' THEN
                IF NEW.mismatch_category IS NOT NULL
                   OR NEW.authoritative_evidence_complete IS FALSE
                   OR NEW.resolution_status <> 'resolved'
                   OR NEW.local_evidence_id IS NULL
                   OR NEW.provider_evidence_id IS NULL
                   OR (
                        NEW.object_type NOT IN (
                            'gateway_fee','refund_fee','adjustment'
                        )
                        AND NEW.settlement_evidence_id IS NULL
                   ) THEN
                    RAISE EXCEPTION
                        'PAY-14 auto resolution requires complete authoritative evidence';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM public.platform_accounting_evidence e
                    WHERE e.id IN (
                        NEW.local_evidence_id,
                        NEW.provider_evidence_id,
                        NEW.settlement_evidence_id
                    )
                      AND e.authoritative IS FALSE
                ) THEN
                    RAISE EXCEPTION
                        'PAY-14 auto resolution requires authoritative evidence';
                END IF;
            END IF;

            IF TG_OP='UPDATE' THEN
                IF ROW(
                    NEW.closure_run_id, NEW.organization_id,
                    NEW.object_type, NEW.reconciliation_key,
                    NEW.local_object_id, NEW.first_detected_at, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.closure_run_id, OLD.organization_id,
                    OLD.object_type, OLD.reconciliation_key,
                    OLD.local_object_id, OLD.first_detected_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION
                        'PAY-14 reconciliation identity is immutable';
                END IF;

                IF OLD.resolution_status='resolved'
                   AND ROW(
                        NEW.resolution_status, NEW.mismatch_category,
                        NEW.safe_outcome, NEW.resolution_evidence_sha256,
                        NEW.resolution_evidence_ref, NEW.resolved_at
                   ) IS DISTINCT FROM ROW(
                        OLD.resolution_status, OLD.mismatch_category,
                        OLD.safe_outcome, OLD.resolution_evidence_sha256,
                        OLD.resolution_evidence_ref, OLD.resolved_at
                   ) THEN
                    RAISE EXCEPTION
                        'PAY-14 resolved reconciliation item is terminal';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_accounting_reconciliation_validate
        BEFORE INSERT OR UPDATE
        ON public.platform_accounting_reconciliation_items
        FOR EACH ROW
        EXECUTE FUNCTION public.pay14_validate_reconciliation_item();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay14_validate_accounting_incident()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            item_run UUID;
            item_org UUID;
            item_outcome TEXT;
            item_status TEXT;
        BEGIN
            IF TG_OP='INSERT' THEN
                SELECT
                    closure_run_id, organization_id,
                    safe_outcome, resolution_status
                INTO item_run, item_org, item_outcome, item_status
                FROM public.platform_accounting_reconciliation_items
                WHERE id=NEW.reconciliation_item_id;

                IF item_run IS DISTINCT FROM NEW.closure_run_id
                   OR item_org IS DISTINCT FROM NEW.organization_id
                   OR item_outcome IS DISTINCT FROM NEW.incident_type
                   OR item_status <> 'incident_open' THEN
                    RAISE EXCEPTION
                        'PAY-14 incident must bind matching reconciliation outcome';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status='resolved'
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-14 resolved accounting incident is terminal';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='open' AND NEW.status IN (
                        'investigating','resolved'
                    ))
                    OR
                    (
                        OLD.status='investigating'
                        AND NEW.status='resolved'
                    )
               ) THEN
                RAISE EXCEPTION
                    'PAY-14 forbidden incident transition: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_accounting_incident_validate
        BEFORE INSERT OR UPDATE
        ON public.platform_accounting_incidents
        FOR EACH ROW
        EXECUTE FUNCTION public.pay14_validate_accounting_incident();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay14_validate_closure_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            unresolved_items BIGINT;
            open_incidents BIGINT;
            actual_items BIGINT;
            actual_mismatches BIGINT;
            actual_retries BIGINT;
            actual_manual BIGINT;
            actual_incidents BIGINT;
            actual_resolved BIGINT;
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.status <> 'collecting' THEN
                    RAISE EXCEPTION
                        'PAY-14 closure must start collecting';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.provider_code, NEW.environment,
                NEW.closure_key, NEW.period_start, NEW.period_end,
                NEW.started_at, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.provider_code, OLD.environment,
                OLD.closure_key, OLD.period_start, OLD.period_end,
                OLD.started_at, OLD.created_at
            ) THEN
                RAISE EXCEPTION
                    'PAY-14 closure identity is immutable';
            END IF;

            IF OLD.status='closed'
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-14 closed accounting period is terminal';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='collecting' AND NEW.status IN (
                        'reconciling','failed'
                    ))
                    OR
                    (OLD.status='reconciling' AND NEW.status IN (
                        'review_required','ready_to_close','failed'
                    ))
                    OR
                    (OLD.status='review_required' AND NEW.status IN (
                        'reconciling','ready_to_close','failed'
                    ))
                    OR
                    (OLD.status='ready_to_close' AND NEW.status IN (
                        'review_required','closed','failed'
                    ))
               ) THEN
                RAISE EXCEPTION
                    'PAY-14 forbidden closure transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status IN ('ready_to_close','closed') THEN
                SELECT
                    count(*),
                    count(*) FILTER (
                        WHERE mismatch_category IS NOT NULL
                    ),
                    count(*) FILTER (
                        WHERE safe_outcome='retry_required'
                    ),
                    count(*) FILTER (
                        WHERE safe_outcome='manual_review_required'
                    ),
                    count(*) FILTER (
                        WHERE safe_outcome IN (
                            'security_incident','accounting_incident'
                        )
                    ),
                    count(*) FILTER (
                        WHERE resolution_status='resolved'
                    ),
                    count(*) FILTER (
                        WHERE resolution_status <> 'resolved'
                    )
                INTO
                    actual_items, actual_mismatches, actual_retries,
                    actual_manual, actual_incidents, actual_resolved,
                    unresolved_items
                FROM public.platform_accounting_reconciliation_items
                WHERE closure_run_id=NEW.id;

                SELECT count(*)
                INTO open_incidents
                FROM public.platform_accounting_incidents
                WHERE closure_run_id=NEW.id
                  AND status <> 'resolved';

                IF unresolved_items <> 0 OR open_incidents <> 0 THEN
                    RAISE EXCEPTION
                        'PAY-14 closure blocked by unresolved reconciliation';
                END IF;

                IF NEW.observed_object_count <> actual_items
                   OR NEW.mismatch_count <> actual_mismatches
                   OR NEW.retry_count <> actual_retries
                   OR NEW.manual_review_count <> actual_manual
                   OR NEW.incident_count <> actual_incidents
                   OR NEW.resolved_count <> actual_resolved THEN
                    RAISE EXCEPTION
                        'PAY-14 closure counters must equal durable item state';
                END IF;

                IF NEW.evidence_manifest_sha256 IS NULL
                   OR NEW.evidence_manifest_ref IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-14 closure requires immutable evidence manifest';
                END IF;

                IF NEW.status='ready_to_close'
                   AND NEW.ready_at IS NULL THEN
                    RAISE EXCEPTION
                        'PAY-14 ready closure requires ready_at';
                END IF;
                IF NEW.status='closed'
                   AND (
                        OLD.status <> 'ready_to_close'
                        OR NEW.closed_at IS NULL
                        OR NEW.closed_by IS NULL
                   ) THEN
                    RAISE EXCEPTION
                        'PAY-14 close requires prior ready state and human/system actor';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_accounting_closure_validate
        BEFORE INSERT OR UPDATE
        ON public.platform_accounting_closure_runs
        FOR EACH ROW
        EXECUTE FUNCTION public.pay14_validate_closure_transition();
        """
    )

    for table_name in (
        "platform_accounting_closure_runs",
        "platform_accounting_reconciliation_items",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_touch_updated_at
            BEFORE UPDATE ON public.{table_name}
            FOR EACH ROW
            EXECUTE FUNCTION public.platform_billing_touch_updated_at();
            """
        )

    for table_name in PAY14_TABLES:
        _enable_rls(table_name)

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname='app_runtime'
            ) THEN
                GRANT SELECT ON
                    public.platform_accounting_closure_runs,
                    public.platform_accounting_evidence,
                    public.platform_accounting_reconciliation_items,
                    public.platform_accounting_incidents
                TO app_runtime;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table_name in PAY14_TABLES:
        op.execute(
            f"ALTER TABLE public.{table_name} NO FORCE ROW LEVEL SECURITY;"
        )

    op.execute(
        """
        DO $pay14_guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.platform_accounting_closure_runs
            )
               OR EXISTS (
                SELECT 1 FROM public.platform_accounting_evidence
            )
               OR EXISTS (
                SELECT 1 FROM public.platform_accounting_reconciliation_items
            )
               OR EXISTS (
                SELECT 1 FROM public.platform_accounting_incidents
            ) THEN
                RAISE EXCEPTION
                    'PAY-14 downgrade blocked: reconciliation/accounting history exists';
            END IF;
        END
        $pay14_guard$;
        """
    )

    for table_name in PAY14_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{table_name} "
            f"ON public.{table_name};"
        )

    for table_name in (
        "platform_accounting_reconciliation_items",
        "platform_accounting_closure_runs",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at "
            f"ON public.{table_name};"
        )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_accounting_closure_validate
        ON public.platform_accounting_closure_runs;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay14_validate_closure_transition();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_accounting_incident_validate
        ON public.platform_accounting_incidents;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay14_validate_accounting_incident();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_accounting_reconciliation_validate
        ON public.platform_accounting_reconciliation_items;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay14_validate_reconciliation_item();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_accounting_evidence_append_only
        ON public.platform_accounting_evidence;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay14_validate_accounting_evidence();"
    )

    op.execute("DROP TABLE IF EXISTS public.platform_accounting_incidents;")
    op.execute(
        "DROP TABLE IF EXISTS public.platform_accounting_reconciliation_items;"
    )
    op.execute("DROP TABLE IF EXISTS public.platform_accounting_evidence;")
    op.execute("DROP TABLE IF EXISTS public.platform_accounting_closure_runs;")
