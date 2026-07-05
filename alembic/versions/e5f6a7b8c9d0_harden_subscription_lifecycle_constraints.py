"""harden subscription lifecycle constraints

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-15 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX ux_subscription_terms_one_normal_renewal_child
        ON subscription_terms (renewed_from_term_id)
        WHERE renewed_from_term_id IS NOT NULL
          AND source_type = 'renewal'
          AND status IN ('pending_payment', 'scheduled', 'active');
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_subscription_term_slots_one_primary
        ON subscription_term_slots (term_id)
        WHERE slot_role = 'primary';
        """
    )

    op.execute(
        """
        ALTER TABLE subscription_terms
        ADD CONSTRAINT ex_subscription_terms_series_reserving_overlap
        EXCLUDE USING gist (
            series_id WITH =,
            daterange(starts_on, effective_ends_on + 1, '[)') WITH &&
        )
        WHERE (status IN ('scheduled', 'active'));
        """
    )
    op.execute(
        """
        ALTER TABLE subscription_slot_assignments
        ADD CONSTRAINT ex_subscription_slot_assignments_slot_overlap
        EXCLUDE USING gist (
            term_slot_id WITH =,
            daterange(effective_from, COALESCE(effective_until + 1, 'infinity'::date), '[)') WITH &&
        )
        WHERE (assignment_state <> 'voided');
        """
    )
    op.execute(
        """
        ALTER TABLE subscription_freezes
        ADD CONSTRAINT ex_subscription_freezes_term_overlap
        EXCLUDE USING gist (
            term_id WITH =,
            daterange(requested_starts_on, COALESCE(planned_ends_on + 1, 'infinity'::date), '[)') WITH &&
        )
        WHERE (status IN ('scheduled', 'active'));
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_subscription_series_tenant()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM members m
                WHERE m.id = NEW.primary_member_id
                  AND m.org_id = NEW.org_id
            ) THEN
                RAISE EXCEPTION 'subscription_series primary member must belong to same organization';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_subscription_series_tenant
        BEFORE INSERT OR UPDATE ON subscription_series
        FOR EACH ROW
        EXECUTE FUNCTION validate_subscription_series_tenant();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_subscription_term_tenant_and_lineage()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_term RECORD;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM membership_plans p
                WHERE p.id = NEW.plan_id
                  AND p.org_id = NEW.org_id
            ) THEN
                RAISE EXCEPTION 'subscription_terms plan must belong to same organization';
            END IF;

            IF NEW.renewed_from_term_id IS NOT NULL THEN
                IF NEW.renewed_from_term_id = NEW.id THEN
                    RAISE EXCEPTION 'subscription_terms cannot renew from itself';
                END IF;

                SELECT t.org_id, t.series_id
                INTO parent_term
                FROM subscription_terms t
                WHERE t.id = NEW.renewed_from_term_id;

                IF parent_term.org_id IS NULL THEN
                    RAISE EXCEPTION 'subscription_terms renewal parent does not exist';
                END IF;

                IF parent_term.org_id <> NEW.org_id THEN
                    RAISE EXCEPTION 'subscription_terms renewal parent must belong to same organization';
                END IF;

                IF parent_term.series_id <> NEW.series_id THEN
                    RAISE EXCEPTION 'subscription_terms renewal parent must belong to same subscription series';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_subscription_term_tenant_and_lineage
        BEFORE INSERT OR UPDATE ON subscription_terms
        FOR EACH ROW
        EXECUTE FUNCTION validate_subscription_term_tenant_and_lineage();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_subscription_assignment_integrity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            term_row RECORD;
            slot_row RECORD;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM members m
                WHERE m.id = NEW.member_id
                  AND m.org_id = NEW.org_id
            ) THEN
                RAISE EXCEPTION 'subscription_slot_assignments member must belong to same organization';
            END IF;

            SELECT t.starts_on, t.effective_ends_on
            INTO term_row
            FROM subscription_terms t
            WHERE t.id = NEW.term_id
              AND t.org_id = NEW.org_id;

            IF term_row.starts_on IS NULL THEN
                RAISE EXCEPTION 'subscription_slot_assignments term must belong to same organization';
            END IF;

            SELECT s.term_id, s.org_id
            INTO slot_row
            FROM subscription_term_slots s
            WHERE s.id = NEW.term_slot_id
              AND s.org_id = NEW.org_id;

            IF slot_row.term_id IS NULL THEN
                RAISE EXCEPTION 'subscription_slot_assignments slot must belong to same organization';
            END IF;

            IF slot_row.term_id <> NEW.term_id THEN
                RAISE EXCEPTION 'subscription_slot_assignments slot must belong to the assigned term';
            END IF;

            IF NEW.effective_from < term_row.starts_on
               OR COALESCE(NEW.effective_until, term_row.effective_ends_on) > term_row.effective_ends_on THEN
                RAISE EXCEPTION 'subscription_slot_assignments dates must fit within subscription term';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_subscription_assignment_integrity
        BEFORE INSERT OR UPDATE ON subscription_slot_assignments
        FOR EACH ROW
        EXECUTE FUNCTION validate_subscription_assignment_integrity();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_subscription_freeze_integrity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            term_row RECORD;
        BEGIN
            SELECT t.series_id, t.starts_on, t.effective_ends_on
            INTO term_row
            FROM subscription_terms t
            WHERE t.id = NEW.term_id
              AND t.org_id = NEW.org_id;

            IF term_row.series_id IS NULL THEN
                RAISE EXCEPTION 'subscription_freezes term must belong to same organization';
            END IF;

            IF term_row.series_id <> NEW.series_id THEN
                RAISE EXCEPTION 'subscription_freezes term must belong to same subscription series';
            END IF;

            IF NEW.requested_starts_on < term_row.starts_on
               OR COALESCE(NEW.planned_ends_on, term_row.effective_ends_on) > term_row.effective_ends_on THEN
                RAISE EXCEPTION 'subscription_freezes dates must fit within subscription term';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_subscription_freeze_integrity
        BEFORE INSERT OR UPDATE ON subscription_freezes
        FOR EACH ROW
        EXECUTE FUNCTION validate_subscription_freeze_integrity();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_validate_subscription_freeze_integrity ON subscription_freezes;")
    op.execute("DROP FUNCTION IF EXISTS validate_subscription_freeze_integrity();")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_subscription_assignment_integrity ON subscription_slot_assignments;")
    op.execute("DROP FUNCTION IF EXISTS validate_subscription_assignment_integrity();")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_subscription_term_tenant_and_lineage ON subscription_terms;")
    op.execute("DROP FUNCTION IF EXISTS validate_subscription_term_tenant_and_lineage();")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_subscription_series_tenant ON subscription_series;")
    op.execute("DROP FUNCTION IF EXISTS validate_subscription_series_tenant();")

    op.execute("ALTER TABLE subscription_freezes DROP CONSTRAINT IF EXISTS ex_subscription_freezes_term_overlap;")
    op.execute(
        "ALTER TABLE subscription_slot_assignments "
        "DROP CONSTRAINT IF EXISTS ex_subscription_slot_assignments_slot_overlap;"
    )
    op.execute(
        "ALTER TABLE subscription_terms "
        "DROP CONSTRAINT IF EXISTS ex_subscription_terms_series_reserving_overlap;"
    )
    op.execute("DROP INDEX IF EXISTS ux_subscription_term_slots_one_primary;")
    op.execute("DROP INDEX IF EXISTS ux_subscription_terms_one_normal_renewal_child;")
