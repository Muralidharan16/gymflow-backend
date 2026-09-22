\set ON_ERROR_STOP on

-- PAY-23 local production evidence collector.
-- Invoke with psql -X -v window_start='...' -v window_end='...'.
-- This script is intentionally aggregate-only and read-only.

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL lock_timeout = '2s';

WITH
params AS (
    SELECT
        :'window_start'::timestamptz AS window_start,
        :'window_end'::timestamptz AS window_end
),
finance_payments AS (
    SELECT
        count(*)::bigint AS finance_count,
        coalesce(sum(round(p.amount * 100)), 0)::bigint AS finance_amount_minor
    FROM finance.payments p, params x
    WHERE p.created_at >= x.window_start
      AND p.created_at < x.window_end
      AND p.provider_payment_ref IS NOT NULL
      AND p.status IN ('captured','partially_refunded','refunded','settled')
),
finance_refunds AS (
    SELECT
        count(*)::bigint AS finance_count,
        coalesce(sum(round(r.amount * 100)), 0)::bigint AS finance_amount_minor
    FROM finance.refunds r, params x
    WHERE r.created_at >= x.window_start
      AND r.created_at < x.window_end
      AND r.status = 'succeeded'
),
finance_settlements AS (
    SELECT
        count(*)::bigint AS finance_count,
        coalesce(
            sum(round((o.payload_json->>'settlement_amount')::numeric * 100)),
            0
        )::bigint AS finance_amount_minor
    FROM finance.outbox_events o, params x
    WHERE o.created_at >= x.window_start
      AND o.created_at < x.window_end
      AND o.aggregate_type = 'payment'
      AND o.event_type = 'finance.payment.reconciled'
),
allocation_rollup AS (
    SELECT
        count(*)::bigint AS allocation_count,
        count(i.id)::bigint AS joined_allocation_count,
        coalesce(sum(round(a.allocated_amount * 100)), 0)::bigint AS allocation_amount_minor,
        coalesce(
            sum(round(CASE WHEN i.id IS NOT NULL THEN a.allocated_amount ELSE 0 END * 100)),
            0
        )::bigint AS joined_invoice_allocation_amount_minor,
        count(*) FILTER (
            WHERE p.organization_id IS DISTINCT FROM i.organization_id
        )::bigint AS cross_tenant_allocation_count
    FROM finance.payment_allocations a
    JOIN finance.payments p ON p.id = a.payment_id
    LEFT JOIN finance.invoices i ON i.id = a.invoice_id
    CROSS JOIN params x
    WHERE p.created_at >= x.window_start
      AND p.created_at < x.window_end
),
invoice_rollup AS (
    SELECT
        count(*) FILTER (
            WHERE i.status IN ('partially_paid','paid')
              AND coalesce(a.allocated_amount, 0) = 0
        )::bigint AS orphaned_invoice_count,
        count(*) FILTER (
            WHERE
                (i.status = 'paid' AND coalesce(a.allocated_amount, 0) <> i.grand_total_amount)
                OR
                (i.status = 'partially_paid' AND (
                    coalesce(a.allocated_amount, 0) <= 0
                    OR coalesce(a.allocated_amount, 0) >= i.grand_total_amount
                ))
                OR
                (i.status = 'issued' AND coalesce(a.allocated_amount, 0) > 0)
        )::bigint AS invoice_state_mismatch_count
    FROM finance.invoices i
    LEFT JOIN (
        SELECT invoice_id, sum(allocated_amount) AS allocated_amount
        FROM finance.payment_allocations
        GROUP BY invoice_id
    ) a ON a.invoice_id = i.id
    CROSS JOIN params x
    WHERE i.created_at >= x.window_start
      AND i.created_at < x.window_end
),
ledger_entry_totals AS (
    SELECT
        e.id,
        coalesce(sum(l.debit_amount), 0) AS debit_total,
        coalesce(sum(l.credit_amount), 0) AS credit_total
    FROM finance.ledger_entries e
    JOIN finance.ledger_entry_lines l ON l.ledger_entry_id = e.id
    CROSS JOIN params x
    WHERE e.created_at >= x.window_start
      AND e.created_at < x.window_end
      AND e.status = 'posted'
    GROUP BY e.id
),
ledger_rollup AS (
    SELECT
        coalesce(sum(round(debit_total * 100)), 0)::bigint AS posted_debit_minor,
        coalesce(sum(round(credit_total * 100)), 0)::bigint AS posted_credit_minor,
        count(*) FILTER (WHERE debit_total <> credit_total)::bigint AS unbalanced_entry_count
    FROM ledger_entry_totals
),
subscription_expected AS (
    SELECT
        count(*)::bigint AS expected_activation_count,
        count(*) FILTER (
            WHERE t.id IS NULL
               OR t.status <> 'active'
               OR t.activated_at IS NULL
        )::bigint AS activation_mismatch_count
    FROM member_subscription_finance_event_consumptions c
    LEFT JOIN subscription_terms t
      ON t.id = c.subscription_term_id
     AND t.org_id = c.org_id
    CROSS JOIN params x
    WHERE c.consumed_at >= x.window_start
      AND c.consumed_at < x.window_end
      AND c.effect_applied IS TRUE
),
subscription_actual AS (
    SELECT count(*)::bigint AS actual_activation_count
    FROM member_subscription_finance_event_consumptions c
    JOIN subscription_terms t
      ON t.id = c.subscription_term_id
     AND t.org_id = c.org_id
    CROSS JOIN params x
    WHERE c.consumed_at >= x.window_start
      AND c.consumed_at < x.window_end
      AND c.effect_applied IS TRUE
      AND t.status = 'active'
      AND t.activated_at IS NOT NULL
),
unexplained_entitlements AS (
    SELECT count(*)::bigint AS unexplained_entitlement_grants
    FROM subscription_terms t
    CROSS JOIN params x
    WHERE t.activated_at >= x.window_start
      AND t.activated_at < x.window_end
      AND t.status = 'active'
      AND t.source_type IN ('admission','renewal')
      AND NOT EXISTS (
          SELECT 1
          FROM member_subscription_finance_event_consumptions c
          WHERE c.subscription_term_id = t.id
            AND c.org_id = t.org_id
            AND c.effect_applied IS TRUE
      )
),
platform_state AS (
    SELECT
        count(*)::bigint AS checked_state_count,
        count(*) FILTER (
            WHERE status NOT IN (
                'trialing','active','past_due','pause_scheduled','paused',
                'cancel_scheduled','canceled','expired'
            )
        )::bigint AS invalid_state_count
    FROM platform_subscriptions
),
platform_recon AS (
    SELECT count(*) FILTER (
        WHERE resolution_status IN ('open','failed')
    )::bigint AS open_reconciliation_discrepancy_count
    FROM platform_reconciliation_items
),
closure AS (
    SELECT
        count(*)::bigint AS live_closed_run_count,
        coalesce(sum(expected_object_count), 0)::bigint AS expected_object_count,
        coalesce(sum(observed_object_count), 0)::bigint AS observed_object_count,
        coalesce(sum(resolved_count), 0)::bigint AS resolved_object_count,
        coalesce(sum(mismatch_count), 0)::bigint AS mismatch_count,
        coalesce(sum(retry_count), 0)::bigint AS retry_count,
        coalesce(sum(manual_review_count), 0)::bigint AS manual_review_count,
        coalesce(sum(incident_count), 0)::bigint AS incident_count
    FROM platform_accounting_closure_runs c, params x
    WHERE c.environment = 'live'
      AND c.status = 'closed'
      AND c.period_start >= x.window_start
      AND c.period_end <= x.window_end
),
duplicate_refunds AS (
    SELECT count(*)::bigint AS duplicate_refunds
    FROM (
        SELECT provider_code, provider_refund_ref
        FROM finance.refund_execution_commands
        WHERE provider_refund_ref IS NOT NULL
        GROUP BY provider_code, provider_refund_ref
        HAVING count(*) > 1
    ) d
),
cross_tenant AS (
    SELECT
        (
            SELECT cross_tenant_allocation_count FROM allocation_rollup
        )
        +
        (
            SELECT count(*)
            FROM finance.refunds r
            JOIN finance.payments p ON p.id = r.payment_id
            WHERE r.organization_id IS DISTINCT FROM p.organization_id
        )::bigint AS unresolved_cross_tenant_anomalies
)
SELECT jsonb_pretty(
    jsonb_build_object(
        'window_start', (SELECT window_start FROM params),
        'window_end', (SELECT window_end FROM params),
        'finance_payments', (SELECT to_jsonb(finance_payments) FROM finance_payments),
        'finance_settlements', (SELECT to_jsonb(finance_settlements) FROM finance_settlements),
        'finance_refunds', (SELECT to_jsonb(finance_refunds) FROM finance_refunds),
        'allocations_invoices',
            (SELECT to_jsonb(allocation_rollup) FROM allocation_rollup)
            || (SELECT to_jsonb(invoice_rollup) FROM invoice_rollup),
        'ledger', (SELECT to_jsonb(ledger_rollup) FROM ledger_rollup),
        'subscriptions',
            (SELECT to_jsonb(subscription_expected) FROM subscription_expected)
            || (SELECT to_jsonb(subscription_actual) FROM subscription_actual)
            || (SELECT to_jsonb(unexplained_entitlements) FROM unexplained_entitlements),
        'platform_billing',
            (SELECT to_jsonb(platform_state) FROM platform_state)
            || (SELECT to_jsonb(platform_recon) FROM platform_recon),
        'accounting_closure', (SELECT to_jsonb(closure) FROM closure),
        'duplicate_refunds', (SELECT duplicate_refunds FROM duplicate_refunds),
        'unresolved_cross_tenant_anomalies',
            (SELECT unresolved_cross_tenant_anomalies FROM cross_tenant)
    )
);

ROLLBACK;
