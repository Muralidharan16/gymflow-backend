# PAY-18 — Financial Observability, Audit and Incident Response

PAY-18 is stacked on frozen PAY-17 and adds **observation only**. It does not add
payment, refund, settlement, entitlement, provider, release, or deployment
authority.

## Authority model

PostgreSQL durable Finance and Platform Billing records remain business truth.
PAY-18 exposes aggregate counts through
`app_secure.pay18_financial_observability_snapshot()`, a STABLE SECURITY
DEFINER function owned by the non-login `app_security_owner`. Only the isolated
`lifecycle_maintenance_runtime` may execute the function. It receives no new
direct table SELECT authority and cannot assume the security owner.

The existing five-minute external-effect observability task reads this aggregate
snapshot and records the required metrics. Invalid webhook signatures never
become durable rows, so `webhook_signature_failure_total` is emitted directly
at the rejection boundary.

## Metric contract

The mandatory metrics are:

```text
payment_attempt_total
payment_failure_total
payment_unknown_total
webhook_signature_failure_total
webhook_backlog
payment_application_backlog
finance_outbox_backlog
refund_backlog
refund_unknown_total
settlement_mismatch_total
reconciliation_open_total
mandate_failure_total
dunning_stage_total
chargeback_open_total
```

PAY-18 also emits `duplicate_payment_allegation_open_total` so a customer
duplicate-charge allegation has an explicit operational signal instead of being
collapsed into a generic dispute count.

No metric label may contain payment, invoice, refund, dispute, customer, tenant,
organization, provider-object, request, correlation, principal, amount,
currency, token, signature, secret, or payload identity. The only PAY-18
dimensions are finite enums: webhook provider/reason, mandate state, and dunning
stage.

## Sanitized correlation logging

Finance logs use `finance_correlation`, derived from the already-sanitized P8
request/task correlation by HMAC-SHA256 using the process secret and truncated
to a bounded `fin-<20 hex>` token. Raw correlation IDs are never copied into
Finance event fields. P8 pre-sink recursive redaction remains mandatory.

Webhook signature failures log only the event category, bounded provider,
bounded rejection reason, and sanitized Finance correlation. Raw signatures,
bodies, payment references, customer identifiers, and provider object IDs are
forbidden.

## Audit

PAY-18 does not create a competing audit ledger. PAY-16
`finance.security_audit_events` remains the tamper-evident chained authority
for privileged Finance actions. Provider/payment/refund/dispute/reconciliation
tables remain the durable financial evidence for their own workflows. Incident
response must preserve this evidence before repair.

## Incident response

The PAY-18 alert bundle binds exactly one runbook for each required incident:
provider outage, webhook outage, unknown payment, duplicate payment allegation,
settlement mismatch, refund ambiguity, chargeback, expired mandate, queue
backlog, database recovery, and credential compromise.

Every runbook requires evidence preservation, ordered diagnosis, safe first
actions, explicit forbidden actions, escalation, and recovery verification.
Blind provider retries, manual terminal-state edits, evidence deletion, and
fencing/idempotency bypass are forbidden.

## Safety posture

```text
PAY18_LIVE_PROVIDER=DISABLED
PAY18_PRODUCTION_CREDENTIALS=DISABLED
PAY18_LIVE_MONEY_MOVEMENT=DISABLED
PAY18_PRODUCTION_RUNTIME_BINDING=DISABLED
PAY18_MERGE=NOT_AUTHORIZED
PAY18_RELEASE=NOT_AUTHORIZED
PAY18_DEPLOYMENT=NOT_AUTHORIZED
```

The phase may be frozen only when the exact-head certification emits
`PAY18_FINANCIAL_OBSERVABILITY=PASS`.
