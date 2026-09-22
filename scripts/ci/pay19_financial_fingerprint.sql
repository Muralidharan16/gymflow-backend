\pset tuples_only on
\pset format unaligned
\set ON_ERROR_STOP on

SELECT 'alembic=' || version_num
FROM alembic_version;

SELECT 'finance_counts=' || concat_ws(',',
    (SELECT count(*) FROM finance.invoices),
    (SELECT count(*) FROM finance.payments),
    (SELECT count(*) FROM finance.provider_operations),
    (SELECT count(*) FROM finance.payment_events),
    (SELECT count(*) FROM finance.payment_application_records),
    (SELECT count(*) FROM finance.payment_allocations),
    (SELECT count(*) FROM finance.ledger_entries),
    (SELECT count(*) FROM finance.ledger_entry_lines),
    (SELECT count(*) FROM finance.refunds),
    (SELECT count(*) FROM finance.refund_execution_commands),
    (SELECT count(*) FROM finance.idempotency_keys),
    (SELECT count(*) FROM finance.outbox_events),
    (SELECT count(*) FROM finance.member_subscription_checkout_bindings)
);

SELECT 'invoice_integrity=' || concat_ws(',',
    (SELECT count(*) FROM (
        SELECT legal_entity_id,gst_registration_id,financial_year,official_invoice_number
        FROM finance.invoices
        WHERE official_invoice_number IS NOT NULL
        GROUP BY 1,2,3,4 HAVING count(*) > 1
    ) AS duplicate_numbers),
    (SELECT coalesce(max(last_number),0) FROM finance.invoice_series),
    (SELECT coalesce(max(
        CASE
          WHEN official_invoice_number ~ '^VS/[0-9]{4}/[0-9]{5}$'
          THEN substring(official_invoice_number from '[0-9]{5}$')::bigint
          ELSE 0
        END
    ),0) FROM finance.invoices),
    (SELECT count(*) FROM finance.invoices
      WHERE official_invoice_number='VS/2425/00001')
);

SELECT 'ledger_integrity=' || concat_ws(',',
    (SELECT count(*) FROM (
        SELECT e.id
        FROM finance.ledger_entries AS e
        JOIN finance.ledger_entry_lines AS l ON l.ledger_entry_id=e.id
        WHERE e.status='posted'
        GROUP BY e.id
        HAVING sum(l.debit_amount) <> sum(l.credit_amount)
    ) AS unbalanced),
    (SELECT coalesce(sum(debit_amount),0)::text FROM finance.ledger_entry_lines),
    (SELECT coalesce(sum(credit_amount),0)::text FROM finance.ledger_entry_lines)
);

SELECT 'provider_integrity=' || concat_ws(',',
    (SELECT count(*) FROM (
        SELECT provider_code,provider_payment_ref
        FROM finance.payments
        WHERE provider_payment_ref IS NOT NULL
        GROUP BY 1,2 HAVING count(*) > 1
    ) AS duplicate_payment_refs),
    (SELECT count(*) FROM (
        SELECT provider_code,environment,provider_object_id
        FROM finance.provider_operations
        WHERE provider_object_id IS NOT NULL
        GROUP BY 1,2,3 HAVING count(*) > 1
    ) AS duplicate_provider_objects),
    (SELECT count(*) FROM finance.provider_operations
      WHERE provider_code='razorpay_sandbox'
        AND environment='sandbox'
        AND operation_type='create_checkout'
        AND status='succeeded'
        AND provider_object_id='order_test_1')
);

SELECT 'refund_integrity=' || concat_ws(',',
    (SELECT count(*) FROM finance.refund_execution_commands),
    (SELECT count(*) FROM finance.refund_execution_commands AS c
      LEFT JOIN finance.refunds AS r ON r.id=c.refund_id
      LEFT JOIN finance.payments AS p ON p.id=c.payment_id
      WHERE r.id IS NULL OR p.id IS NULL
         OR c.organization_id IS DISTINCT FROM r.organization_id
         OR c.payment_id IS DISTINCT FROM r.payment_id
         OR c.amount IS DISTINCT FROM r.amount
         OR c.currency_code IS DISTINCT FROM r.currency_code),
    (SELECT count(*) FROM (
       SELECT logical_obligation_key
       FROM finance.refund_execution_commands
       GROUP BY 1 HAVING count(*) > 1
    ) AS duplicate_obligations)
);

SELECT 'idempotency_integrity=' || concat_ws(',',
    (SELECT count(*) FROM (
      SELECT organization_id,scope,idempotency_key
      FROM finance.idempotency_keys
      GROUP BY 1,2,3 HAVING count(*) > 1
    ) AS duplicate_keys),
    (SELECT count(*) FROM finance.idempotency_keys
      WHERE request_hash_sha256 !~ '^[0-9a-f]{64}$'),
    (SELECT count(*) FROM (
      SELECT organization_id,scope,idempotency_key
      FROM finance.monetary_commands
      GROUP BY 1,2,3 HAVING count(*) > 1
    ) AS duplicate_monetary_commands)
);

SELECT 'outbox_integrity=' || concat_ws(',',
    (SELECT count(*) FROM (
      SELECT aggregate_type,aggregate_id,event_type,idempotency_key
      FROM finance.outbox_events
      GROUP BY 1,2,3,4 HAVING count(*) > 1
    ) AS duplicate_outbox),
    (SELECT count(*) FROM finance.outbox_events
      WHERE payload_sha256 !~ '^[0-9a-f]{64}$'),
    (SELECT count(*) FROM finance.outbox_events
      WHERE (status='processing') IS DISTINCT FROM
            (leased_by IS NOT NULL AND leased_until IS NOT NULL))
);

SELECT 'subscription_binding_integrity=' || concat_ws(',',
    (SELECT count(*) FROM finance.member_subscription_checkout_bindings),
    (SELECT count(*)
     FROM finance.member_subscription_checkout_bindings AS b
     LEFT JOIN public.member_subscriptions_v2 AS s
       ON s.id=b.subscription_id AND s.org_id=b.organization_id
     LEFT JOIN finance.invoices AS i
       ON i.id=b.invoice_id AND i.organization_id=b.organization_id
     LEFT JOIN finance.payments AS p
       ON p.id=b.checkout_intent_id AND p.organization_id=b.organization_id
     WHERE s.id IS NULL OR i.id IS NULL OR p.id IS NULL),
    (SELECT count(*) FROM (
      SELECT subscription_id FROM finance.member_subscription_checkout_bindings
      GROUP BY 1 HAVING count(*) > 1
    ) AS duplicate_subscription_bindings)
);

SELECT 'payment_application_integrity=' || concat_ws(',',
    (SELECT count(*) FROM finance.payment_application_records),
    (SELECT count(*) FROM (
      SELECT payment_event_id
      FROM finance.payment_application_records
      GROUP BY 1 HAVING count(*) > 1
    ) AS duplicate_application_events),
    (SELECT count(*)
     FROM finance.payment_application_records AS a
     LEFT JOIN finance.payments AS p
       ON p.id=a.payment_id AND p.organization_id=a.organization_id
     LEFT JOIN finance.invoices AS i
       ON i.id=a.invoice_id AND i.organization_id=a.organization_id
     WHERE p.id IS NULL OR (a.invoice_id IS NOT NULL AND i.id IS NULL))
);

SELECT 'stable_digest=' || md5(concat_ws('|',
    coalesce((SELECT string_agg(
        id::text || ':' || coalesce(official_invoice_number,'') || ':' || status,
        ',' ORDER BY id
    ) FROM finance.invoices),''),
    coalesce((SELECT string_agg(
        id::text || ':' || provider_code || ':' || coalesce(provider_payment_ref,'') || ':' || status,
        ',' ORDER BY id
    ) FROM finance.payments),''),
    coalesce((SELECT string_agg(
        id::text || ':' || status || ':' || coalesce(provider_object_id,''),
        ',' ORDER BY id
    ) FROM finance.provider_operations),''),
    coalesce((SELECT string_agg(
        id::text || ':' || payment_event_id::text || ':' || decision_code,
        ',' ORDER BY id
    ) FROM finance.payment_application_records),''),
    coalesce((SELECT string_agg(
        command_id::text || ':' || refund_id::text || ':' || logical_obligation_key || ':' || status,
        ',' ORDER BY command_id
    ) FROM finance.refund_execution_commands),''),
    coalesce((SELECT string_agg(
        id::text || ':' || aggregate_type || ':' || event_type || ':' || status,
        ',' ORDER BY id
    ) FROM finance.outbox_events),''),
    coalesce((SELECT string_agg(
        id::text || ':' || subscription_id::text || ':' || invoice_id::text || ':' || checkout_intent_id::text,
        ',' ORDER BY id
    ) FROM finance.member_subscription_checkout_bindings),'')
));
