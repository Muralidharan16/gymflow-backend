# PAY-13 — Disputes, Chargebacks and Financial Exceptions

## Scope

PAY-13 adds a dedicated Platform Billing dispute and financial-exception layer on top of the exact certified PAY-12 candidate.

Exact predecessor:

- PAY-12 SHA: `60549fcb5bee4649d229e8e99b056d6d378f479a`
- PAY-12 tree: `dbf5cb9cc59a83e8d2d36ebe1e3ac29371b497e2`
- Alembic predecessor: `zx07d8e9f0a58`
- PAY-13 Alembic head: `zy07d8e9f0a59`

PAY-13 does not authorize a live provider, production credentials, live money movement, production dispute-runtime binding, merge, release, or deployment.

## Historical payment truth

A dispute is not a failed payment.

If a payment was captured, that fact remains:

```text
payment succeeded
      ↓
later dispute / chargeback
      ↓
separate dispute liability and outcome
```

PAY-13 never rewrites a captured payment to `failed`. Once a dispute is attached to a captured payment, the database explicitly rejects a later attempt to rewrite that successful payment status.

## Dispute aggregate

Canonical states:

```text
opened
  ↓
evidence_required
  ↓
submitted
  ↓
under_review
  ↙       ↘
won       lost
  \       /
    closed
```

The aggregate records the mapped payment, invoice, provider release, provider dispute identity, amount/currency, current provider evidence, timestamps, provider decision reference, and a financial hold.

Supported dispute categories:

- chargeback;
- cardholder dispute;
- duplicate-charge allegation;
- fraud review.

A provider dispute that cannot be mapped safely to a captured Platform Billing payment does **not** create a tenant dispute by guessing. It becomes a quarantined `unmapped_dispute` exception.

## Financial hold

An unresolved dispute freezes new mutable cash actions on the disputed payment/invoice:

- no new payment attempt on the disputed invoice;
- no new refund request;
- no refund approval/provider execution while the hold is active.

Previously initiated provider operations may still record their terminal provider truth. Corrective evidence/documents remain possible.

The hold is released only with a durable provider decision.

## Dispute liability and financial closure

PAY-13 adds append-only `platform_dispute_financial_entries`.

Entry types:

```text
liability_recognized
liability_reversed
loss_recognized
loss_reversed
```

At dispute open, the same transaction must record `liability_recognized`.

A deferred PostgreSQL constraint verifies that before commit.

For a provider decision:

```text
won  -> liability_reversed
lost -> loss_recognized
```

Those financial entries are also required in the **same transaction** as the status decision. A crash cannot commit `won` without reversing the reserve, or `lost` without recording the loss.

A later chargeback reversal after a recorded loss does not rewrite the earlier provider decision. PAY-13 appends `loss_reversed`, preserving:

```text
payment captured
chargeback lost
loss recognized
later chargeback reversal received
loss reversed
```

## Evidence and history

`platform_dispute_evidence` is append-only and deduplicated by evidence digest per dispute.

Supported evidence categories include provider notices, customer statements, invoice/payment receipts, service-delivery evidence, fraud signals, provider decisions, and chargeback reversals.

`platform_dispute_events` is an append-only, contiguous per-dispute event stream. Webhook and reconciliation application of the same normalized provider fact is deduplicated by evidence digest and event type.

Moving to `submitted` requires durable evidence to already exist.

## Financial exceptions

The following cases are quarantined rather than auto-mutated:

- accidental duplicate provider payment;
- orphan provider payment;
- orphan settlement;
- unknown refund;
- wrong customer mapping;
- unmapped provider dispute.

`platform_financial_exception_cases` permits `organization_id = NULL` while ownership is unresolved. Forced RLS means such rows are not exposed through tenant runtime reads.

Exception lifecycle:

```text
detected / quarantined
        ↓
investigating
        ↓
mapped
        ↓
resolved / ignored
```

All exception rows are permanently:

- `manual_review_required = true`;
- `automatic_financial_mutation_allowed = false`.

Wrong-customer mapping and orphan cases therefore cannot trigger automatic refund, collection, allocation, or entitlement changes.

## Provider ingress

PAY-13 accepts normalized financial facts only from:

- signature-verified webhook processing;
- reconciliation;
- explicit manual-review evidence.

Provider adapters normalize facts into the PAY-13 domain contract. The same canonical evidence SHA must identify the same financial fact across webhook and reconciliation.

No raw provider payload is stored in dispute tables.

## Tenant and authority model

Mapped dispute tables are tenant-scoped and forced-RLS protected.

Application runtime receives read-only access. PAY-13 does not bind a production write/runtime identity.

Unmapped exception rows are invisible to a tenant because their organization is unresolved.

## Migration safety

PAY-13 is additive.

An empty PAY-13 schema must round-trip:

`PAY-12 -> PAY-13 -> PAY-12 -> PAY-13`.

Downgrade fails closed when any dispute, evidence, event, dispute financial entry, or financial-exception history exists.

## Certification

The exact PAY-13 candidate must prove:

- dispute transition matrix and terminality;
- original captured payment truth cannot be rewritten;
- opening a dispute requires a captured payment;
- dispute amount/currency/provider identity are bound to captured payment truth;
- duplicate provider dispute facts are idempotent;
- active dispute blocks new payment/refund money actions;
- append-only evidence/events/financial entries;
- contiguous dispute event sequencing;
- same-transaction dispute liability recognition;
- same-transaction won/lost financial closure;
- chargeback reversal appends loss reversal without rewriting historical decision;
- duplicate-charge allegation and fraud-review paths;
- orphan payment/settlement, unknown refund and wrong-customer mapping quarantine;
- unresolved exceptions cannot automatically mutate money;
- tenant RLS and unresolved-row isolation;
- PG16 lifecycle and populated downgrade fail-closed;
- Platform Billing, Finance, architecture/security and migration regression suites.

Gate:

`PAY13_DISPUTE_EXCEPTION_HANDLING=PASS`
