"""PAY-12 recurring payments, mandates and dunning

Revision ID: zx07d8e9f0a58
Revises: zw07d8e9f0a57
Create Date: 2026-09-21 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "zx07d8e9f0a58"
down_revision: Union[str, Sequence[str], None] = "zw07d8e9f0a57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES = (
    "platform_recurring_billing_jobs",
    "platform_dunning_cases",
    "platform_dunning_attempts",
    "platform_notification_deliveries",
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
        ALTER TABLE public.platform_mandates
            DROP CONSTRAINT chk_platform_mandates_status,
            ADD CONSTRAINT chk_platform_mandates_status
                CHECK (status IN ('pending','authorized','active','paused','revoked','expired','failed')),
            ADD COLUMN payment_rail TEXT NOT NULL DEFAULT 'legacy_provider_recurring',
            ADD COLUMN authorized_at TIMESTAMPTZ NULL,
            ADD COLUMN paused_at TIMESTAMPTZ NULL,
            ADD COLUMN expired_at TIMESTAMPTZ NULL,
            ADD COLUMN failed_at TIMESTAMPTZ NULL,
            ADD COLUMN failure_code VARCHAR(100) NULL,
            ADD COLUMN replacement_mandate_id UUID NULL,
            ADD COLUMN replaced_at TIMESTAMPTZ NULL,
            ADD CONSTRAINT chk_platform_mandates_payment_rail
                CHECK (
                    payment_rail IN (
                        'upi_autopay','e_mandate','card_recurring',
                        'legacy_provider_recurring'
                    )
                ),
            ADD CONSTRAINT chk_platform_mandates_failure_code
                CHECK (failure_code IS NULL OR failure_code ~ '^[a-z0-9_]+$'),
            ADD CONSTRAINT chk_platform_mandates_replacement_shape
                CHECK (
                    (replacement_mandate_id IS NULL AND replaced_at IS NULL)
                    OR (replacement_mandate_id IS NOT NULL AND replaced_at IS NOT NULL)
                ),
            ADD CONSTRAINT chk_platform_mandates_replacement_not_self
                CHECK (replacement_mandate_id IS NULL OR replacement_mandate_id <> id),
            ADD CONSTRAINT fk_platform_mandates_replacement_org
                FOREIGN KEY (replacement_mandate_id, organization_id)
                REFERENCES public.platform_mandates(id, organization_id)
                ON DELETE RESTRICT;
        """
    )

    op.execute(
        """
        ALTER TABLE public.platform_invoices
            ADD COLUMN service_period_start TIMESTAMPTZ NULL,
            ADD COLUMN service_period_end TIMESTAMPTZ NULL,
            ADD CONSTRAINT chk_platform_invoices_service_period_pair
                CHECK (
                    (service_period_start IS NULL AND service_period_end IS NULL)
                    OR (
                        service_period_start IS NOT NULL
                        AND service_period_end IS NOT NULL
                        AND service_period_end > service_period_start
                    )
                );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_invoices_subscription_service_period
        ON public.platform_invoices (
            subscription_id, service_period_start, service_period_end
        )
        WHERE subscription_id IS NOT NULL
          AND service_period_start IS NOT NULL
          AND service_period_end IS NOT NULL
          AND status <> 'void';
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_recurring_billing_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            period_start TIMESTAMPTZ NOT NULL,
            period_end TIMESTAMPTZ NOT NULL,
            run_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            lease_owner UUID NULL,
            lease_until TIMESTAMPTZ NULL,
            lease_fence BIGINT NOT NULL DEFAULT 0,
            invoice_id UUID NULL,
            last_payment_attempt_id UUID NULL,
            next_attempt_at TIMESTAMPTZ NULL,
            last_error_code VARCHAR(80) NULL,
            last_evidence_sha256 CHAR(64) NULL,
            last_evidence_ref VARCHAR(300) NULL,
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_recurring_jobs_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_recurring_jobs_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_recurring_jobs_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_recurring_jobs_attempt_org
                FOREIGN KEY (last_payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_recurring_jobs_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_recurring_jobs_period
                UNIQUE (subscription_id, period_start, period_end),
            CONSTRAINT chk_platform_recurring_jobs_period_order
                CHECK (period_end > period_start),
            CONSTRAINT chk_platform_recurring_jobs_max_attempts
                CHECK (max_attempts > 0),
            CONSTRAINT chk_platform_recurring_jobs_attempt_count
                CHECK (attempt_count BETWEEN 0 AND max_attempts),
            CONSTRAINT chk_platform_recurring_jobs_status
                CHECK (
                    status IN (
                        'scheduled','processing','awaiting_reconciliation',
                        'retry','completed','dead_lettered','canceled'
                    )
                ),
            CONSTRAINT chk_platform_recurring_jobs_lease_pair
                CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
            CONSTRAINT chk_platform_recurring_jobs_lease_fence
                CHECK (lease_fence >= 0),
            CONSTRAINT chk_platform_recurring_jobs_error_code
                CHECK (last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_]+$'),
            CONSTRAINT chk_platform_recurring_jobs_evidence_sha
                CHECK (
                    last_evidence_sha256 IS NULL
                    OR last_evidence_sha256 ~ '^[0-9a-f]{64}$'
                ),
            CONSTRAINT chk_platform_recurring_jobs_terminal_completed
                CHECK (
                    status NOT IN ('completed','dead_lettered','canceled')
                    OR completed_at IS NOT NULL
                )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_recurring_jobs_due
        ON public.platform_recurring_billing_jobs (
            status, run_at, next_attempt_at
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_recurring_jobs_org_subscription
        ON public.platform_recurring_billing_jobs (
            organization_id, subscription_id
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_dunning_cases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            subscription_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            policy_code VARCHAR(80) NOT NULL,
            policy_snapshot_json JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            stage TEXT NOT NULL DEFAULT 'full_grace',
            first_confirmed_failure_at TIMESTAMPTZ NOT NULL,
            full_grace_ends_at TIMESTAMPTZ NOT NULL,
            limited_write_ends_at TIMESTAMPTZ NOT NULL,
            read_only_ends_at TIMESTAMPTZ NOT NULL,
            confirmed_attempt_count INTEGER NOT NULL DEFAULT 1,
            max_attempts INTEGER NOT NULL,
            next_retry_at TIMESTAMPTZ NULL,
            last_evidence_kind VARCHAR(80) NOT NULL,
            last_evidence_sha256 CHAR(64) NOT NULL,
            last_evidence_ref VARCHAR(300) NOT NULL,
            last_evidence_at TIMESTAMPTZ NOT NULL,
            restricted_at TIMESTAMPTZ NULL,
            suspended_at TIMESTAMPTZ NULL,
            recovered_at TIMESTAMPTZ NULL,
            terminated_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            version BIGINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_platform_dunning_cases_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dunning_cases_subscription_org
                FOREIGN KEY (subscription_id, organization_id)
                REFERENCES public.platform_subscriptions(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dunning_cases_invoice_org
                FOREIGN KEY (invoice_id, organization_id)
                REFERENCES public.platform_invoices(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_dunning_cases_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT chk_platform_dunning_cases_status
                CHECK (status IN ('open','suspended','recovered','terminated')),
            CONSTRAINT chk_platform_dunning_cases_stage
                CHECK (
                    stage IN (
                        'full_grace','limited_write','read_only',
                        'billing_only','recovered'
                    )
                ),
            CONSTRAINT chk_platform_dunning_cases_policy_snapshot
                CHECK (jsonb_typeof(policy_snapshot_json) = 'object'),
            CONSTRAINT chk_platform_dunning_cases_attempt_count
                CHECK (confirmed_attempt_count BETWEEN 1 AND max_attempts),
            CONSTRAINT chk_platform_dunning_cases_max_attempts
                CHECK (max_attempts > 0),
            CONSTRAINT chk_platform_dunning_cases_boundaries
                CHECK (
                    full_grace_ends_at >= first_confirmed_failure_at
                    AND limited_write_ends_at >= full_grace_ends_at
                    AND read_only_ends_at >= limited_write_ends_at
                ),
            CONSTRAINT chk_platform_dunning_cases_durable_evidence_kind
                CHECK (
                    last_evidence_kind IN (
                        'confirmed_payment_failure',
                        'mandate_unavailable',
                        'customer_action_required',
                        'payment_succeeded'
                    )
                ),
            CONSTRAINT chk_platform_dunning_cases_evidence_sha
                CHECK (last_evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dunning_cases_evidence_ref
                CHECK (btrim(last_evidence_ref) <> ''),
            CONSTRAINT chk_platform_dunning_cases_recovered_shape
                CHECK (
                    status <> 'recovered'
                    OR (stage='recovered' AND recovered_at IS NOT NULL)
                ),
            CONSTRAINT chk_platform_dunning_cases_suspended_shape
                CHECK (status <> 'suspended' OR suspended_at IS NOT NULL),
            CONSTRAINT chk_platform_dunning_cases_terminated_shape
                CHECK (status <> 'terminated' OR terminated_at IS NOT NULL),
            CONSTRAINT chk_platform_dunning_cases_version
                CHECK (version >= 1)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_dunning_cases_open_invoice
        ON public.platform_dunning_cases (invoice_id)
        WHERE status IN ('open','suspended');
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_dunning_cases_org_status
        ON public.platform_dunning_cases (organization_id, status, stage);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_dunning_cases_next_retry
        ON public.platform_dunning_cases (next_retry_at)
        WHERE status='open';
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_dunning_attempts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            dunning_case_id UUID NOT NULL,
            payment_attempt_id UUID NULL,
            attempt_number INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            counts_toward_dunning BOOLEAN NOT NULL,
            evidence_sha256 CHAR(64) NOT NULL,
            evidence_ref VARCHAR(300) NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            next_retry_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_dunning_attempts_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dunning_attempts_case_org
                FOREIGN KEY (dunning_case_id, organization_id)
                REFERENCES public.platform_dunning_cases(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_platform_dunning_attempts_payment_org
                FOREIGN KEY (payment_attempt_id, organization_id)
                REFERENCES public.platform_payment_attempts(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_dunning_attempts_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_dunning_attempts_number
                UNIQUE (dunning_case_id, attempt_number),
            CONSTRAINT uq_platform_dunning_attempts_evidence
                UNIQUE (dunning_case_id, evidence_sha256),
            CONSTRAINT chk_platform_dunning_attempts_number
                CHECK (attempt_number > 0),
            CONSTRAINT chk_platform_dunning_attempts_outcome
                CHECK (
                    outcome IN (
                        'confirmed_payment_failure','mandate_unavailable',
                        'customer_action_required','payment_succeeded'
                    )
                ),
            CONSTRAINT chk_platform_dunning_attempts_counting
                CHECK (
                    (outcome='payment_succeeded' AND counts_toward_dunning IS FALSE)
                    OR
                    (outcome<>'payment_succeeded' AND counts_toward_dunning IS TRUE)
                ),
            CONSTRAINT chk_platform_dunning_attempts_evidence_sha
                CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_dunning_attempts_evidence_ref
                CHECK (btrim(evidence_ref) <> '')
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_dunning_attempts_case
        ON public.platform_dunning_attempts (
            dunning_case_id, attempt_number
        );
        """
    )

    op.execute(
        """
        CREATE TABLE public.platform_notification_deliveries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            dunning_case_id UUID NULL,
            notification_type VARCHAR(80) NOT NULL,
            policy_code VARCHAR(80) NOT NULL,
            channel TEXT NOT NULL,
            recipient_hash CHAR(64) NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            scheduled_at TIMESTAMPTZ NOT NULL,
            sent_at TIMESTAMPTZ NULL,
            provider_message_id VARCHAR(200) NULL,
            dedupe_key VARCHAR(180) NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_safe TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_platform_notification_deliveries_organization
                FOREIGN KEY (organization_id)
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_platform_notification_deliveries_case_org
                FOREIGN KEY (dunning_case_id, organization_id)
                REFERENCES public.platform_dunning_cases(id, organization_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_platform_notification_deliveries_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_platform_notification_deliveries_dedupe
                UNIQUE (organization_id, dedupe_key),
            CONSTRAINT chk_platform_notification_deliveries_type
                CHECK (
                    notification_type IN (
                        'payment_failed','retry_scheduled','grace_ending',
                        'access_limited','access_read_only',
                        'subscription_suspended','payment_recovered',
                        'mandate_expiring','mandate_revoked',
                        'payment_method_replaced'
                    )
                ),
            CONSTRAINT chk_platform_notification_deliveries_channel
                CHECK (channel IN ('email','sms','in_app','whatsapp')),
            CONSTRAINT chk_platform_notification_deliveries_status
                CHECK (
                    status IN ('queued','sending','sent','failed','suppressed')
                ),
            CONSTRAINT chk_platform_notification_deliveries_recipient_hash
                CHECK (recipient_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_platform_notification_deliveries_dedupe
                CHECK (btrim(dedupe_key) <> ''),
            CONSTRAINT chk_platform_notification_deliveries_attempt_count
                CHECK (attempt_count >= 0),
            CONSTRAINT chk_platform_notification_deliveries_sent_shape
                CHECK (status <> 'sent' OR sent_at IS NOT NULL)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_notification_deliveries_due
        ON public.platform_notification_deliveries (status, scheduled_at);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_platform_notification_deliveries_org
        ON public.platform_notification_deliveries (
            organization_id, created_at
        );
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_protect_invoice_service_period()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status <> 'draft'
               AND (
                    NEW.service_period_start IS DISTINCT FROM OLD.service_period_start
                    OR NEW.service_period_end IS DISTINCT FROM OLD.service_period_end
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 issued invoice service period is immutable';
            END IF;

            IF OLD.status = 'draft'
               AND NEW.status = 'issued'
               AND NEW.subscription_id IS NOT NULL
               AND (
                    NEW.service_period_start IS NULL
                    OR NEW.service_period_end IS NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 recurring subscription invoice requires service period before issuance';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_invoices_pay12_service_period
        BEFORE UPDATE OF status, service_period_start, service_period_end
        ON public.platform_invoices
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_protect_invoice_service_period();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_validate_mandate_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' THEN
                    RAISE EXCEPTION 'PAY-12 mandates must be created pending';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status IN ('revoked','expired','failed')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-12 terminal mandate cannot transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='pending' AND NEW.status IN ('authorized','failed','revoked'))
                    OR
                    (OLD.status='authorized' AND NEW.status IN ('active','paused','revoked','expired','failed'))
                    OR
                    (OLD.status='active' AND NEW.status IN ('paused','revoked','expired','failed'))
                    OR
                    (OLD.status='paused' AND NEW.status IN ('active','revoked','expired','failed'))
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 forbidden mandate transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            IF NEW.status='authorized' AND NEW.authorized_at IS NULL THEN
                RAISE EXCEPTION
                    'PAY-12 authorized mandate requires authorized_at';
            END IF;
            IF NEW.status='active' AND NEW.activated_at IS NULL THEN
                RAISE EXCEPTION
                    'PAY-12 active mandate requires activated_at';
            END IF;
            IF NEW.status='paused' AND NEW.paused_at IS NULL THEN
                RAISE EXCEPTION
                    'PAY-12 paused mandate requires paused_at';
            END IF;
            IF NEW.status='revoked' AND NEW.revoked_at IS NULL THEN
                RAISE EXCEPTION
                    'PAY-12 revoked mandate requires revoked_at';
            END IF;
            IF NEW.status='expired' AND NEW.expired_at IS NULL THEN
                RAISE EXCEPTION
                    'PAY-12 expired mandate requires expired_at';
            END IF;
            IF NEW.status='failed'
               AND (NEW.failed_at IS NULL OR NEW.failure_code IS NULL) THEN
                RAISE EXCEPTION
                    'PAY-12 failed mandate requires failed_at and failure_code';
            END IF;

            IF OLD.replacement_mandate_id IS NOT NULL
               AND NEW.replacement_mandate_id IS DISTINCT FROM OLD.replacement_mandate_id THEN
                RAISE EXCEPTION
                    'PAY-12 replacement mandate binding is immutable once set';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_mandates_pay12_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_mandates
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_validate_mandate_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_validate_recurring_job_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'scheduled'
                   OR NEW.attempt_count <> 0
                   OR NEW.lease_owner IS NOT NULL
                   OR NEW.lease_until IS NOT NULL
                   OR NEW.lease_fence <> 0
                   OR NEW.completed_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'PAY-12 recurring jobs must start scheduled and unleased';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.subscription_id,
                NEW.period_start, NEW.period_end, NEW.run_at,
                NEW.max_attempts, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.subscription_id,
                OLD.period_start, OLD.period_end, OLD.run_at,
                OLD.max_attempts, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'PAY-12 recurring job identity is immutable';
            END IF;

            IF OLD.status IN ('completed','dead_lettered','canceled')
               AND ROW(
                    NEW.status, NEW.invoice_id, NEW.last_payment_attempt_id,
                    NEW.last_evidence_sha256, NEW.last_evidence_ref
               ) IS DISTINCT FROM ROW(
                    OLD.status, OLD.invoice_id, OLD.last_payment_attempt_id,
                    OLD.last_evidence_sha256, OLD.last_evidence_ref
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 terminal recurring job financial facts are immutable';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='scheduled' AND NEW.status IN ('processing','canceled'))
                    OR
                    (OLD.status='processing' AND NEW.status IN ('retry','awaiting_reconciliation','completed','dead_lettered'))
                    OR
                    (OLD.status='retry' AND NEW.status IN ('processing','dead_lettered','canceled'))
                    OR
                    (OLD.status='awaiting_reconciliation' AND NEW.status IN ('processing','retry','completed','dead_lettered'))
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 forbidden recurring job transition: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_recurring_jobs_pay12_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_recurring_billing_jobs
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_validate_recurring_job_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_validate_dunning_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_rank INTEGER;
            new_rank INTEGER;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'open'
                   OR NEW.stage <> 'full_grace'
                   OR NEW.last_evidence_kind NOT IN (
                        'confirmed_payment_failure',
                        'mandate_unavailable',
                        'customer_action_required'
                   ) THEN
                    RAISE EXCEPTION
                        'PAY-12 dunning cases require first confirmed durable failure evidence';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.subscription_id, NEW.invoice_id,
                NEW.policy_code, NEW.policy_snapshot_json,
                NEW.first_confirmed_failure_at, NEW.full_grace_ends_at,
                NEW.limited_write_ends_at, NEW.read_only_ends_at,
                NEW.max_attempts, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.subscription_id, OLD.invoice_id,
                OLD.policy_code, OLD.policy_snapshot_json,
                OLD.first_confirmed_failure_at, OLD.full_grace_ends_at,
                OLD.limited_write_ends_at, OLD.read_only_ends_at,
                OLD.max_attempts, OLD.created_at
            ) THEN
                RAISE EXCEPTION
                    'PAY-12 dunning identity and policy snapshot are immutable';
            END IF;

            IF NEW.confirmed_attempt_count < OLD.confirmed_attempt_count
               OR NEW.confirmed_attempt_count > OLD.confirmed_attempt_count + 1 THEN
                RAISE EXCEPTION
                    'PAY-12 confirmed dunning attempt count must advance monotonically one at a time';
            END IF;

            IF NEW.confirmed_attempt_count > OLD.confirmed_attempt_count
               AND NEW.last_evidence_kind NOT IN (
                    'confirmed_payment_failure',
                    'mandate_unavailable',
                    'customer_action_required'
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 dunning attempt advance requires confirmed durable failure evidence';
            END IF;

            IF OLD.first_confirmed_failure_at
               IS DISTINCT FROM NEW.first_confirmed_failure_at THEN
                RAISE EXCEPTION
                    'PAY-12 first confirmed failure timestamp is immutable';
            END IF;

            IF NEW.last_evidence_kind IN (
                'provider_outage','provider_timeout','provider_unknown'
            ) THEN
                RAISE EXCEPTION
                    'PAY-12 provider outage evidence cannot create or advance dunning';
            END IF;

            IF OLD.status IN ('recovered','terminated')
               AND ROW(
                    NEW.status, NEW.stage, NEW.first_confirmed_failure_at,
                    NEW.confirmed_attempt_count
               ) IS DISTINCT FROM ROW(
                    OLD.status, OLD.stage, OLD.first_confirmed_failure_at,
                    OLD.confirmed_attempt_count
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 terminal dunning case cannot be rewritten';
            END IF;

            old_rank := CASE OLD.stage
                WHEN 'full_grace' THEN 1
                WHEN 'limited_write' THEN 2
                WHEN 'read_only' THEN 3
                WHEN 'billing_only' THEN 4
                WHEN 'recovered' THEN 5
                ELSE 0
            END;
            new_rank := CASE NEW.stage
                WHEN 'full_grace' THEN 1
                WHEN 'limited_write' THEN 2
                WHEN 'read_only' THEN 3
                WHEN 'billing_only' THEN 4
                WHEN 'recovered' THEN 5
                ELSE 0
            END;

            IF NEW.stage <> 'recovered' AND new_rank < old_rank THEN
                RAISE EXCEPTION
                    'PAY-12 dunning stage cannot move backward';
            END IF;
            IF NEW.stage='recovered'
               AND NEW.last_evidence_kind <> 'payment_succeeded' THEN
                RAISE EXCEPTION
                    'PAY-12 dunning recovery requires durable payment success evidence';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_dunning_cases_pay12_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_dunning_cases
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_validate_dunning_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_protect_dunning_attempt()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            case_max_attempts INTEGER;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'PAY-12 dunning attempts are append-only';
            END IF;

            SELECT max_attempts
            INTO case_max_attempts
            FROM public.platform_dunning_cases
            WHERE id = NEW.dunning_case_id
              AND organization_id = NEW.organization_id;

            IF case_max_attempts IS NULL THEN
                RAISE EXCEPTION 'PAY-12 dunning attempt case does not exist';
            END IF;
            IF NEW.attempt_number > case_max_attempts THEN
                RAISE EXCEPTION 'PAY-12 dunning attempt exceeds policy max attempts';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_dunning_attempts_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_dunning_attempts
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_protect_dunning_attempt();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.pay12_validate_notification_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status NOT IN ('queued','suppressed')
                   OR NEW.attempt_count <> 0 THEN
                    RAISE EXCEPTION
                        'PAY-12 notifications must start queued or suppressed';
                END IF;
                RETURN NEW;
            END IF;

            IF ROW(
                NEW.organization_id, NEW.dunning_case_id,
                NEW.notification_type, NEW.policy_code, NEW.channel,
                NEW.recipient_hash, NEW.scheduled_at,
                NEW.dedupe_key, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.dunning_case_id,
                OLD.notification_type, OLD.policy_code, OLD.channel,
                OLD.recipient_hash, OLD.scheduled_at,
                OLD.dedupe_key, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'PAY-12 notification identity is immutable';
            END IF;

            IF OLD.status IN ('sent','suppressed')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'PAY-12 terminal notification state cannot revert';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (
                    (OLD.status='queued' AND NEW.status IN ('sending','suppressed'))
                    OR
                    (OLD.status='sending' AND NEW.status IN ('sent','failed','queued'))
                    OR
                    (OLD.status='failed' AND NEW.status IN ('queued','suppressed'))
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 forbidden notification transition: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_platform_notification_deliveries_pay12_lifecycle
        BEFORE INSERT OR UPDATE ON public.platform_notification_deliveries
        FOR EACH ROW
        EXECUTE FUNCTION public.pay12_validate_notification_transition();
        """
    )

    for table_name in (
        "platform_recurring_billing_jobs",
        "platform_dunning_cases",
        "platform_notification_deliveries",
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
                    public.platform_recurring_billing_jobs,
                    public.platform_dunning_cases,
                    public.platform_dunning_attempts,
                    public.platform_notification_deliveries
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
        DO $pay12_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.platform_recurring_billing_jobs)
               OR EXISTS (SELECT 1 FROM public.platform_dunning_cases)
               OR EXISTS (SELECT 1 FROM public.platform_dunning_attempts)
               OR EXISTS (SELECT 1 FROM public.platform_notification_deliveries)
               OR EXISTS (
                    SELECT 1
                    FROM public.platform_mandates
                    WHERE status IN ('authorized','paused')
                       OR payment_rail <> 'legacy_provider_recurring'
                       OR authorized_at IS NOT NULL
                       OR paused_at IS NOT NULL
                       OR expired_at IS NOT NULL
                       OR failed_at IS NOT NULL
                       OR failure_code IS NOT NULL
                       OR replacement_mandate_id IS NOT NULL
                       OR replaced_at IS NOT NULL
               )
               OR EXISTS (
                    SELECT 1
                    FROM public.platform_invoices
                    WHERE service_period_start IS NOT NULL
                       OR service_period_end IS NOT NULL
               ) THEN
                RAISE EXCEPTION
                    'PAY-12 downgrade blocked: recurring/dunning production history exists';
            END IF;
        END
        $pay12_guard$;
        """
    )

    for table_name in TENANT_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{table_name} "
            f"ON public.{table_name};"
        )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_invoices_pay12_service_period
        ON public.platform_invoices;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_protect_invoice_service_period();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_dunning_attempts_append_only
        ON public.platform_dunning_attempts;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_protect_dunning_attempt();"
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS
            trg_platform_notification_deliveries_pay12_lifecycle
        ON public.platform_notification_deliveries;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_validate_notification_transition();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_dunning_cases_pay12_lifecycle
        ON public.platform_dunning_cases;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_validate_dunning_transition();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_recurring_jobs_pay12_lifecycle
        ON public.platform_recurring_billing_jobs;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_validate_recurring_job_transition();"
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_platform_mandates_pay12_lifecycle
        ON public.platform_mandates;
        """
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.pay12_validate_mandate_transition();"
    )

    for table_name in (
        "platform_notification_deliveries",
        "platform_dunning_cases",
        "platform_recurring_billing_jobs",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_touch_updated_at "
            f"ON public.{table_name};"
        )

    op.execute("DROP TABLE IF EXISTS public.platform_notification_deliveries;")
    op.execute("DROP TABLE IF EXISTS public.platform_dunning_attempts;")
    op.execute("DROP TABLE IF EXISTS public.platform_dunning_cases;")
    op.execute("DROP TABLE IF EXISTS public.platform_recurring_billing_jobs;")

    op.execute(
        "DROP INDEX IF EXISTS public.ux_platform_invoices_subscription_service_period;"
    )
    op.execute(
        """
        ALTER TABLE public.platform_invoices
            DROP CONSTRAINT IF EXISTS chk_platform_invoices_service_period_pair,
            DROP COLUMN IF EXISTS service_period_end,
            DROP COLUMN IF EXISTS service_period_start;
        """
    )

    op.execute(
        """
        ALTER TABLE public.platform_mandates
            DROP CONSTRAINT IF EXISTS fk_platform_mandates_replacement_org,
            DROP CONSTRAINT IF EXISTS chk_platform_mandates_replacement_not_self,
            DROP CONSTRAINT IF EXISTS chk_platform_mandates_replacement_shape,
            DROP CONSTRAINT IF EXISTS chk_platform_mandates_failure_code,
            DROP CONSTRAINT IF EXISTS chk_platform_mandates_payment_rail,
            DROP CONSTRAINT IF EXISTS chk_platform_mandates_status,
            DROP COLUMN IF EXISTS replaced_at,
            DROP COLUMN IF EXISTS replacement_mandate_id,
            DROP COLUMN IF EXISTS failure_code,
            DROP COLUMN IF EXISTS failed_at,
            DROP COLUMN IF EXISTS expired_at,
            DROP COLUMN IF EXISTS paused_at,
            DROP COLUMN IF EXISTS authorized_at,
            DROP COLUMN IF EXISTS payment_rail,
            ADD CONSTRAINT chk_platform_mandates_status
                CHECK (status IN ('pending','active','revoked','expired','failed'));
        """
    )
