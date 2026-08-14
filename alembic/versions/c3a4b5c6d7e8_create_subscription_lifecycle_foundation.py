"""create subscription lifecycle foundation

Revision ID: c3a4b5c6d7e8
Revises: b1c2d3e4f5a6
Create Date: 2026-06-15 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # btree_gist is infrastructure-owned. Alembic validates the exact
    # prerequisite instead of creating or adopting an extension.
    op.execute("""
        DO $btree_gist_infrastructure$
        DECLARE
            owner_name TEXT;
            schema_name TEXT;
        BEGIN
            SELECT
                pg_catalog.pg_get_userbyid(extension_data.extowner),
                namespace_data.nspname::text
            INTO owner_name, schema_name
            FROM pg_catalog.pg_extension AS extension_data
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = extension_data.extnamespace
            WHERE extension_data.extname = 'btree_gist';

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'c3a4b5c6d7e8 requires infrastructure-provisioned btree_gist; Alembic must not create extensions';
            END IF;
            IF owner_name IS DISTINCT FROM 'postgres'
               OR schema_name IS DISTINCT FROM 'public' THEN
                RAISE EXCEPTION
                    'c3a4b5c6d7e8 requires btree_gist owned by postgres in public; owner=%, schema=%',
                    owner_name, schema_name;
            END IF;
        END
        $btree_gist_infrastructure$;
    """)

    op.execute("CREATE TYPE subscription_series_status AS ENUM ('open', 'closed', 'archived');")
    op.execute(
        "CREATE TYPE subscription_term_status AS ENUM "
        "('draft', 'pending_payment', 'scheduled', 'active', 'expired', 'cancelled', 'terminated', 'voided');"
    )
    op.execute(
        "CREATE TYPE subscription_term_source AS ENUM "
        "('admission', 'renewal', 'migration', 'admin_adjustment', 'plan_change', 're_enrolment', 'administrative_correction');"
    )
    op.execute(
        "CREATE TYPE subscription_slot_role AS ENUM "
        "('primary', 'partner', 'dependent', 'family_member', 'corporate_member', 'standard');"
    )
    op.execute("CREATE TYPE subscription_assignment_state AS ENUM ('active', 'released', 'voided');")
    op.execute("CREATE TYPE subscription_freeze_status AS ENUM ('scheduled', 'active', 'completed', 'cancelled');")
    op.execute(
        "CREATE TYPE subscription_event_type AS ENUM "
        "('series_opened', 'admission_created', 'term_scheduled', 'term_activated', "
        "'renewal_created', 'renewal_scheduled', 'term_expired', 'term_cancelled', "
        "'term_terminated', 'term_voided', 'freeze_scheduled', 'freeze_started', "
        "'freeze_resumed', 'freeze_cancelled', 'series_closed', 'series_archived', "
        "'series_restored', 'slot_assigned', 'slot_released');"
    )

    op.execute(
        """
        CREATE TABLE subscription_series (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            originating_branch_id UUID NULL,
            series_code VARCHAR(80) NOT NULL,
            primary_member_id UUID NOT NULL,
            lifecycle_status subscription_series_status NOT NULL DEFAULT 'open',
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            opened_by UUID NULL,
            closed_at TIMESTAMPTZ NULL,
            closed_by UUID NULL,
            closure_reason TEXT NULL,
            archived_at TIMESTAMPTZ NULL,
            archived_by UUID NULL,
            archive_reason TEXT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            version INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT fk_subscription_series_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_series_origin_branch_org
                FOREIGN KEY (originating_branch_id, org_id)
                REFERENCES org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_series_primary_member
                FOREIGN KEY (primary_member_id) REFERENCES members(id) ON DELETE RESTRICT,
            CONSTRAINT uq_subscription_series_id_org UNIQUE (id, org_id),
            CONSTRAINT uq_subscription_series_org_code UNIQUE (org_id, series_code),
            CONSTRAINT chk_subscription_series_version_positive CHECK (version >= 1),
            CONSTRAINT chk_subscription_series_closed_metadata
                CHECK (lifecycle_status <> 'closed' OR closed_at IS NOT NULL),
            CONSTRAINT chk_subscription_series_archived_metadata
                CHECK (lifecycle_status <> 'archived' OR archived_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_series_org_status ON subscription_series (org_id, lifecycle_status);")
    op.execute("CREATE INDEX ix_subscription_series_org_member ON subscription_series (org_id, primary_member_id);")
    op.execute("CREATE INDEX ix_subscription_series_org_branch ON subscription_series (org_id, originating_branch_id);")

    op.execute(
        """
        CREATE TABLE subscription_terms (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            series_id UUID NOT NULL,
            sequence_number INTEGER NOT NULL,
            term_code VARCHAR(80) NOT NULL,
            renewed_from_term_id UUID NULL,
            source_type subscription_term_source NOT NULL,
            plan_id UUID NOT NULL,
            legacy_member_subscription_v2_id UUID NULL,
            legacy_subscription_code VARCHAR(50) NULL,
            plan_code_snapshot VARCHAR(50) NOT NULL,
            plan_name_snapshot VARCHAR(255) NOT NULL,
            duration_unit_snapshot duration_unit NOT NULL,
            duration_value_snapshot INTEGER NOT NULL,
            capacity_snapshot INTEGER NOT NULL,
            currency_code VARCHAR(3) NOT NULL,
            list_price_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            tax_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            final_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
            starts_on DATE NOT NULL,
            base_ends_on DATE NOT NULL,
            effective_ends_on DATE NOT NULL,
            status subscription_term_status NOT NULL,
            activated_at TIMESTAMPTZ NULL,
            expired_at TIMESTAMPTZ NULL,
            cancelled_at TIMESTAMPTZ NULL,
            cancelled_by UUID NULL,
            cancellation_reason TEXT NULL,
            terminated_at TIMESTAMPTZ NULL,
            terminated_by UUID NULL,
            termination_reason TEXT NULL,
            voided_at TIMESTAMPTZ NULL,
            voided_by UUID NULL,
            void_reason TEXT NULL,
            source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            version INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT fk_subscription_terms_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_terms_series_org
                FOREIGN KEY (series_id, org_id) REFERENCES subscription_series(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_terms_branch_org
                FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_terms_plan
                FOREIGN KEY (plan_id) REFERENCES membership_plans(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_terms_renewed_from
                FOREIGN KEY (renewed_from_term_id) REFERENCES subscription_terms(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_terms_legacy_v2
                FOREIGN KEY (legacy_member_subscription_v2_id) REFERENCES member_subscriptions_v2(id) ON DELETE RESTRICT,
            CONSTRAINT uq_subscription_terms_id_org UNIQUE (id, org_id),
            CONSTRAINT uq_subscription_terms_org_code UNIQUE (org_id, term_code),
            CONSTRAINT uq_subscription_terms_series_sequence UNIQUE (series_id, sequence_number),
            CONSTRAINT uq_subscription_terms_legacy_v2 UNIQUE (legacy_member_subscription_v2_id),
            CONSTRAINT chk_subscription_terms_sequence_positive CHECK (sequence_number >= 1),
            CONSTRAINT chk_subscription_terms_duration_positive CHECK (duration_value_snapshot > 0),
            CONSTRAINT chk_subscription_terms_capacity_positive CHECK (capacity_snapshot >= 1),
            CONSTRAINT chk_subscription_terms_dates_order CHECK (starts_on <= base_ends_on AND starts_on <= effective_ends_on),
            CONSTRAINT chk_subscription_terms_amounts_nonnegative
                CHECK (list_price_amount >= 0 AND discount_amount >= 0 AND tax_amount >= 0 AND final_amount >= 0),
            CONSTRAINT chk_subscription_terms_version_positive CHECK (version >= 1),
            CONSTRAINT chk_subscription_terms_not_self_renewed CHECK (renewed_from_term_id IS NULL OR renewed_from_term_id <> id),
            CONSTRAINT chk_subscription_terms_expired_metadata CHECK (status <> 'expired' OR expired_at IS NOT NULL),
            CONSTRAINT chk_subscription_terms_cancelled_metadata CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL),
            CONSTRAINT chk_subscription_terms_terminated_metadata CHECK (status <> 'terminated' OR terminated_at IS NOT NULL),
            CONSTRAINT chk_subscription_terms_voided_metadata CHECK (status <> 'voided' OR voided_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_terms_org_status ON subscription_terms (org_id, status);")
    op.execute("CREATE INDEX ix_subscription_terms_org_branch ON subscription_terms (org_id, branch_id);")
    op.execute("CREATE INDEX ix_subscription_terms_series ON subscription_terms (series_id, sequence_number);")
    op.execute("CREATE INDEX ix_subscription_terms_legacy_source ON subscription_terms (legacy_member_subscription_v2_id);")
    op.execute("CREATE INDEX ix_subscription_terms_org_plan ON subscription_terms (org_id, plan_id);")

    op.execute(
        """
        CREATE TABLE subscription_term_slots (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            term_id UUID NOT NULL,
            slot_index INTEGER NOT NULL,
            slot_role subscription_slot_role NOT NULL DEFAULT 'standard',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_subscription_term_slots_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_term_slots_term_org
                FOREIGN KEY (term_id, org_id) REFERENCES subscription_terms(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT uq_subscription_term_slots_id_org UNIQUE (id, org_id),
            CONSTRAINT uq_subscription_term_slots_term_index UNIQUE (term_id, slot_index),
            CONSTRAINT chk_subscription_term_slots_index_positive CHECK (slot_index >= 1)
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_term_slots_org_term ON subscription_term_slots (org_id, term_id);")

    op.execute(
        """
        CREATE TABLE subscription_slot_assignments (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            term_id UUID NOT NULL,
            term_slot_id UUID NOT NULL,
            member_id UUID NOT NULL,
            effective_from DATE NOT NULL,
            effective_until DATE NULL,
            assignment_state subscription_assignment_state NOT NULL DEFAULT 'active',
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            assigned_by UUID NULL,
            released_at TIMESTAMPTZ NULL,
            released_by UUID NULL,
            release_reason TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_subscription_slot_assignments_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_slot_assignments_term_org
                FOREIGN KEY (term_id, org_id) REFERENCES subscription_terms(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_slot_assignments_slot_org
                FOREIGN KEY (term_slot_id, org_id) REFERENCES subscription_term_slots(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_slot_assignments_member
                FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE RESTRICT,
            CONSTRAINT chk_subscription_slot_assignments_dates
                CHECK (effective_until IS NULL OR effective_from <= effective_until),
            CONSTRAINT chk_subscription_slot_assignments_release_metadata
                CHECK (assignment_state <> 'released' OR released_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_slot_assignments_org_member ON subscription_slot_assignments (org_id, member_id);")
    op.execute("CREATE INDEX ix_subscription_slot_assignments_slot ON subscription_slot_assignments (term_slot_id);")
    op.execute("CREATE INDEX ix_subscription_slot_assignments_term ON subscription_slot_assignments (term_id);")

    op.execute(
        """
        CREATE TABLE subscription_freezes (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            series_id UUID NOT NULL,
            term_id UUID NOT NULL,
            status subscription_freeze_status NOT NULL,
            requested_starts_on DATE NOT NULL,
            planned_ends_on DATE NULL,
            actual_ended_on DATE NULL,
            extension_days INTEGER NOT NULL DEFAULT 0,
            extension_policy VARCHAR(40) NOT NULL DEFAULT 'extend_expiry',
            reason TEXT NULL,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            requested_by UUID NULL,
            approved_at TIMESTAMPTZ NULL,
            approved_by UUID NULL,
            resumed_at TIMESTAMPTZ NULL,
            resumed_by UUID NULL,
            cancelled_at TIMESTAMPTZ NULL,
            cancelled_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_subscription_freezes_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_freezes_series_org
                FOREIGN KEY (series_id, org_id) REFERENCES subscription_series(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_freezes_term_org
                FOREIGN KEY (term_id, org_id) REFERENCES subscription_terms(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT chk_subscription_freezes_extension_nonnegative CHECK (extension_days >= 0),
            CONSTRAINT chk_subscription_freezes_dates CHECK (planned_ends_on IS NULL OR requested_starts_on <= planned_ends_on),
            CONSTRAINT chk_subscription_freezes_completed_metadata CHECK (status <> 'completed' OR actual_ended_on IS NOT NULL),
            CONSTRAINT chk_subscription_freezes_cancelled_metadata CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_freezes_org_term ON subscription_freezes (org_id, term_id);")
    op.execute("CREATE INDEX ix_subscription_freezes_org_status ON subscription_freezes (org_id, status);")

    op.execute(
        """
        CREATE TABLE subscription_events (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            branch_id UUID NULL,
            series_id UUID NOT NULL,
            term_id UUID NULL,
            event_type subscription_event_type NOT NULL,
            event_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_user_id UUID NULL,
            event_source VARCHAR(50) NOT NULL DEFAULT 'system',
            correlation_id VARCHAR(100) NULL,
            idempotency_key VARCHAR(200) NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            before_snapshot JSONB NULL,
            after_snapshot JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_subscription_events_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_events_branch_org
                FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_events_series_org
                FOREIGN KEY (series_id, org_id) REFERENCES subscription_series(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_subscription_events_term_org
                FOREIGN KEY (term_id, org_id) REFERENCES subscription_terms(id, org_id) ON DELETE RESTRICT
        );
        """
    )
    op.execute("CREATE INDEX ix_subscription_events_org_series_time ON subscription_events (org_id, series_id, event_at DESC);")
    op.execute("CREATE INDEX ix_subscription_events_org_term_time ON subscription_events (org_id, term_id, event_at DESC);")

    op.execute(
        """
        CREATE TABLE subscription_operation_idempotency (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL,
            operation_name VARCHAR(80) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            request_hash VARCHAR(128) NOT NULL,
            processing_state VARCHAR(30) NOT NULL,
            resource_type VARCHAR(80) NULL,
            resource_id UUID NULL,
            response_status INTEGER NULL,
            response_snapshot JSONB NULL,
            error_code VARCHAR(120) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_subscription_operation_idempotency_org
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE RESTRICT,
            CONSTRAINT uq_subscription_operation_idempotency_key UNIQUE (org_id, operation_name, idempotency_key),
            CONSTRAINT chk_subscription_operation_idempotency_state
                CHECK (processing_state IN ('processing', 'completed', 'failed', 'expired')),
            CONSTRAINT chk_subscription_operation_idempotency_response_status
                CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_subscription_operation_idempotency_expiry "
        "ON subscription_operation_idempotency (expires_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscription_operation_idempotency;")
    op.execute("DROP TABLE IF EXISTS subscription_events;")
    op.execute("DROP TABLE IF EXISTS subscription_freezes;")
    op.execute("DROP TABLE IF EXISTS subscription_slot_assignments;")
    op.execute("DROP TABLE IF EXISTS subscription_term_slots;")
    op.execute("DROP TABLE IF EXISTS subscription_terms;")
    op.execute("DROP TABLE IF EXISTS subscription_series;")

    op.execute("DROP TYPE IF EXISTS subscription_event_type;")
    op.execute("DROP TYPE IF EXISTS subscription_freeze_status;")
    op.execute("DROP TYPE IF EXISTS subscription_assignment_state;")
    op.execute("DROP TYPE IF EXISTS subscription_slot_role;")
    op.execute("DROP TYPE IF EXISTS subscription_term_source;")
    op.execute("DROP TYPE IF EXISTS subscription_term_status;")
    op.execute("DROP TYPE IF EXISTS subscription_series_status;")
