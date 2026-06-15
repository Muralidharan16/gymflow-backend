"""backfill subscription lifecycle from v2

Revision ID: d4e5f6a7b8c9
Revises: c3a4b5c6d7e8
Create Date: 2026-06-15 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp.subscription_lifecycle_uuid(seed TEXT)
        RETURNS UUID
        LANGUAGE SQL
        IMMUTABLE
        AS $$
            SELECT (
                substr(md5(seed), 1, 8) || '-' ||
                substr(md5(seed), 9, 4) || '-' ||
                substr(md5(seed), 13, 4) || '-' ||
                substr(md5(seed), 17, 4) || '-' ||
                substr(md5(seed), 21, 12)
            )::uuid;
        $$;
        """
    )

    op.execute(
        """
        DO $$
        DECLARE
            over_capacity_count INTEGER;
            invalid_slot_count INTEGER;
            missing_primary_count INTEGER;
        BEGIN
            SELECT count(*) INTO over_capacity_count
            FROM (
                SELECT s.id
                FROM member_subscriptions_v2 s
                JOIN subscription_members sm ON sm.subscription_id = s.id
                GROUP BY s.id, s.max_members_snapshot
                HAVING count(*) > s.max_members_snapshot
            ) anomalies;

            IF over_capacity_count > 0 THEN
                RAISE EXCEPTION 'subscription lifecycle backfill blocked: % source subscriptions exceed capacity', over_capacity_count;
            END IF;

            SELECT count(*) INTO invalid_slot_count
            FROM subscription_members sm
            JOIN member_subscriptions_v2 s ON s.id = sm.subscription_id
            WHERE sm.slot_number < 1
               OR sm.slot_number > s.max_members_snapshot;

            IF invalid_slot_count > 0 THEN
                RAISE EXCEPTION 'subscription lifecycle backfill blocked: % source subscription members have invalid slot numbers', invalid_slot_count;
            END IF;

            SELECT count(*) INTO missing_primary_count
            FROM member_subscriptions_v2 s
            WHERE NOT EXISTS (
                SELECT 1
                FROM subscription_members sm
                WHERE sm.subscription_id = s.id
                  AND sm.member_id = s.primary_member_id
                  AND sm.role = 'primary'
            );

            IF missing_primary_count > 0 THEN
                RAISE EXCEPTION 'subscription lifecycle backfill blocked: % source subscriptions are missing primary-member slot rows', missing_primary_count;
            END IF;
        END$$;
        """
    )

    op.execute(
        """
        INSERT INTO subscription_series (
            id,
            org_id,
            originating_branch_id,
            series_code,
            primary_member_id,
            lifecycle_status,
            opened_at,
            closed_at,
            closure_reason,
            metadata,
            created_at,
            updated_at,
            version
        )
        SELECT
            pg_temp.subscription_lifecycle_uuid('subscription_series:' || s.id::text),
            s.org_id,
            s.branch_id,
            'SER-' || s.subscription_code,
            s.primary_member_id,
            CASE
                WHEN s.status IN ('pending', 'active', 'frozen') AND s.end_date >= CURRENT_DATE THEN 'open'::subscription_series_status
                ELSE 'closed'::subscription_series_status
            END,
            s.created_at,
            CASE
                WHEN s.status IN ('pending', 'active', 'frozen') AND s.end_date >= CURRENT_DATE THEN NULL
                ELSE COALESCE(s.cancelled_at, s.archived_at, s.updated_at, now())
            END,
            CASE
                WHEN s.status IN ('pending', 'active', 'frozen') AND s.end_date >= CURRENT_DATE THEN NULL
                ELSE 'migration_status_' || s.status::text
            END,
            jsonb_build_object(
                'source_table', 'member_subscriptions_v2',
                'source_id', s.id::text,
                'source_status', s.status::text,
                'migration_policy', 'one_series_per_v2_subscription'
            ),
            s.created_at,
            s.updated_at,
            1
        FROM member_subscriptions_v2 s
        WHERE NOT EXISTS (
            SELECT 1
            FROM subscription_series existing
            WHERE existing.org_id = s.org_id
              AND existing.series_code = 'SER-' || s.subscription_code
        );
        """
    )

    op.execute(
        """
        INSERT INTO subscription_terms (
            id,
            org_id,
            branch_id,
            series_id,
            sequence_number,
            term_code,
            renewed_from_term_id,
            source_type,
            plan_id,
            legacy_member_subscription_v2_id,
            legacy_subscription_code,
            plan_code_snapshot,
            plan_name_snapshot,
            duration_unit_snapshot,
            duration_value_snapshot,
            capacity_snapshot,
            currency_code,
            list_price_amount,
            discount_amount,
            tax_amount,
            final_amount,
            starts_on,
            base_ends_on,
            effective_ends_on,
            status,
            activated_at,
            expired_at,
            cancelled_at,
            cancellation_reason,
            source_metadata,
            created_at,
            created_by,
            updated_at,
            version
        )
        SELECT
            pg_temp.subscription_lifecycle_uuid('subscription_term:' || s.id::text),
            s.org_id,
            s.branch_id,
            series.id,
            1,
            s.subscription_code,
            NULL,
            'migration'::subscription_term_source,
            s.membership_plan_id,
            s.id,
            s.subscription_code,
            p.plan_code,
            p.name,
            s.duration_unit_snapshot,
            s.duration_value_snapshot,
            s.max_members_snapshot,
            s.currency_code,
            s.price_snapshot,
            0,
            0,
            s.price_snapshot,
            s.start_date,
            s.end_date,
            s.end_date,
            CASE
                WHEN s.status = 'pending' THEN 'draft'::subscription_term_status
                WHEN s.status = 'active' AND s.end_date < CURRENT_DATE THEN 'expired'::subscription_term_status
                WHEN s.status = 'active' THEN 'active'::subscription_term_status
                WHEN s.status = 'expired' THEN 'expired'::subscription_term_status
                WHEN s.status = 'cancelled' THEN 'cancelled'::subscription_term_status
                WHEN s.status = 'frozen' AND s.end_date < CURRENT_DATE THEN 'expired'::subscription_term_status
                WHEN s.status = 'frozen' THEN 'active'::subscription_term_status
                WHEN s.status = 'archived' THEN 'cancelled'::subscription_term_status
            END,
            CASE
                WHEN s.status IN ('active', 'frozen') AND s.end_date >= CURRENT_DATE THEN s.created_at
                ELSE NULL
            END,
            CASE
                WHEN s.status IN ('expired') OR (s.status IN ('active', 'frozen') AND s.end_date < CURRENT_DATE) THEN COALESCE(s.updated_at, now())
                ELSE NULL
            END,
            CASE
                WHEN s.status IN ('cancelled', 'archived') THEN COALESCE(s.cancelled_at, s.archived_at, s.updated_at, now())
                ELSE NULL
            END,
            CASE
                WHEN s.status IN ('cancelled', 'archived') THEN 'migration_status_' || s.status::text
                ELSE NULL
            END,
            jsonb_build_object(
                'source_table', 'member_subscriptions_v2',
                'source_id', s.id::text,
                'source_status', s.status::text,
                'source_subscription_code', s.subscription_code,
                'date_status_reconciled', (s.status IN ('active', 'frozen') AND s.end_date < CURRENT_DATE)
            ),
            s.created_at,
            s.created_by,
            s.updated_at,
            1
        FROM member_subscriptions_v2 s
        JOIN membership_plans p ON p.id = s.membership_plan_id
        JOIN subscription_series series
          ON series.org_id = s.org_id
         AND series.series_code = 'SER-' || s.subscription_code
        WHERE NOT EXISTS (
            SELECT 1
            FROM subscription_terms existing
            WHERE existing.legacy_member_subscription_v2_id = s.id
        );
        """
    )

    op.execute(
        """
        INSERT INTO subscription_term_slots (
            id,
            org_id,
            term_id,
            slot_index,
            slot_role,
            created_at,
            updated_at
        )
        SELECT
            pg_temp.subscription_lifecycle_uuid('subscription_slot:' || t.id::text || ':' || slot_index::text),
            t.org_id,
            t.id,
            slot_index,
            CASE WHEN slot_index = 1 THEN 'primary'::subscription_slot_role ELSE 'standard'::subscription_slot_role END,
            t.created_at,
            t.updated_at
        FROM subscription_terms t
        CROSS JOIN LATERAL generate_series(1, t.capacity_snapshot) AS slot_index
        WHERE t.source_type = 'migration'
          AND NOT EXISTS (
              SELECT 1
              FROM subscription_term_slots existing
              WHERE existing.term_id = t.id
                AND existing.slot_index = slot_index
          );
        """
    )

    op.execute(
        """
        INSERT INTO subscription_slot_assignments (
            id,
            org_id,
            term_id,
            term_slot_id,
            member_id,
            effective_from,
            effective_until,
            assignment_state,
            assigned_at,
            released_at,
            release_reason,
            created_at,
            updated_at
        )
        SELECT
            pg_temp.subscription_lifecycle_uuid('subscription_assignment:' || sm.id::text),
            t.org_id,
            t.id,
            slot.id,
            sm.member_id,
            t.starts_on,
            COALESCE(sm.left_at::date, t.effective_ends_on),
            CASE WHEN sm.is_active THEN 'active'::subscription_assignment_state ELSE 'released'::subscription_assignment_state END,
            sm.joined_at,
            CASE WHEN sm.is_active THEN NULL ELSE COALESCE(sm.left_at, t.updated_at, now()) END,
            CASE WHEN sm.is_active THEN NULL ELSE 'migration_source_inactive' END,
            sm.created_at,
            sm.updated_at
        FROM subscription_members sm
        JOIN subscription_terms t ON t.legacy_member_subscription_v2_id = sm.subscription_id
        JOIN subscription_term_slots slot
          ON slot.term_id = t.id
         AND slot.slot_index = sm.slot_number
        WHERE NOT EXISTS (
            SELECT 1
            FROM subscription_slot_assignments existing
            WHERE existing.id = pg_temp.subscription_lifecycle_uuid('subscription_assignment:' || sm.id::text)
        );
        """
    )

    op.execute(
        """
        DO $$
        DECLARE
            source_subscription_count INTEGER;
            series_count INTEGER;
            term_count INTEGER;
            source_capacity_total INTEGER;
            slot_count INTEGER;
            source_member_count INTEGER;
            assignment_count INTEGER;
        BEGIN
            SELECT count(*) INTO source_subscription_count FROM member_subscriptions_v2;
            SELECT count(*) INTO series_count
            FROM subscription_series
            WHERE metadata->>'source_table' = 'member_subscriptions_v2';
            SELECT count(*) INTO term_count
            FROM subscription_terms
            WHERE source_type = 'migration'
              AND legacy_member_subscription_v2_id IS NOT NULL;
            SELECT COALESCE(sum(max_members_snapshot), 0) INTO source_capacity_total
            FROM member_subscriptions_v2;
            SELECT count(*) INTO slot_count
            FROM subscription_term_slots slots
            JOIN subscription_terms terms ON terms.id = slots.term_id
            WHERE terms.source_type = 'migration';
            SELECT count(*) INTO source_member_count FROM subscription_members;
            SELECT count(*) INTO assignment_count
            FROM subscription_slot_assignments assignments
            JOIN subscription_terms terms ON terms.id = assignments.term_id
            WHERE terms.source_type = 'migration';

            IF series_count <> source_subscription_count THEN
                RAISE EXCEPTION 'subscription lifecycle reconciliation failed: series count %, source count %', series_count, source_subscription_count;
            END IF;
            IF term_count <> source_subscription_count THEN
                RAISE EXCEPTION 'subscription lifecycle reconciliation failed: term count %, source count %', term_count, source_subscription_count;
            END IF;
            IF slot_count <> source_capacity_total THEN
                RAISE EXCEPTION 'subscription lifecycle reconciliation failed: slot count %, source capacity %', slot_count, source_capacity_total;
            END IF;
            IF assignment_count <> source_member_count THEN
                RAISE EXCEPTION 'subscription lifecycle reconciliation failed: assignment count %, source member count %', assignment_count, source_member_count;
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE TEMP TABLE _subscription_lifecycle_migrated_terms
        ON COMMIT DROP
        AS
        SELECT id, series_id
        FROM subscription_terms
        WHERE source_type = 'migration';
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _subscription_lifecycle_migrated_series
        ON COMMIT DROP
        AS
        SELECT id
        FROM subscription_series
        WHERE metadata->>'source_table' = 'member_subscriptions_v2';
        """
    )
    op.execute(
        """
        DELETE FROM subscription_slot_assignments assignments
        USING _subscription_lifecycle_migrated_terms terms
        WHERE assignments.term_id = terms.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_freezes freezes
        USING _subscription_lifecycle_migrated_terms terms
        WHERE freezes.term_id = terms.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_events events
        USING _subscription_lifecycle_migrated_terms terms
        WHERE events.term_id = terms.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_events events
        USING _subscription_lifecycle_migrated_series series
        WHERE events.series_id = series.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_term_slots slots
        USING _subscription_lifecycle_migrated_terms terms
        WHERE slots.term_id = terms.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_terms terms
        USING _subscription_lifecycle_migrated_terms migrated
        WHERE terms.id = migrated.id;
        """
    )
    op.execute(
        """
        DELETE FROM subscription_series series
        USING _subscription_lifecycle_migrated_series migrated
        WHERE series.id = migrated.id;
        """
    )
