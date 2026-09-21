# PAY-14 — Reconciliation, Treasury and Accounting Closure

## Status

PAY-14 is stacked on the exact certified PAY-13 candidate:

- PAY-13 SHA: `ad4f495f0f79b7e3624de398e6bbfc90a4f55785`
- PAY-13 tree: `3f481a7b163271a44f8be89c4d4fc45130d90f51`
- Alembic predecessor: `zy07d8e9f0a59`
- PAY-14 Alembic head: `zz07d8e9f0a60`

PAY-14 does not authorize live provider credentials, live money movement, production runtime binding, merge, release, or deployment.

## Purpose

PAY-14 proves that Platform Billing financial truth agrees with provider and settlement/bank evidence.

The reconciliation model is three-way:

```text
internal Platform Billing financial state
            ↕
provider payment/refund/dispute state
            ↕
settlement / bank evidence
```

This is **Platform Billing** reconciliation for Gym/Studio → DOers SaaS billing. It does not reuse or merge the separate member-payment Finance Core ledger or member-commerce records.

## Existing reconciliation remains separate

Earlier Platform Billing reconciliation recovers provider-operation state from authoritative provider evidence. PAY-14 does not replace it.

PAY-14 adds an accounting-closure layer above captured payment/refund/dispute truth. It compares evidence and closes accounting periods. It does not become payment/refund/dispute mutation authority.

## Reconciled object types

PAY-14 supports:

- captured payments;
- settlements;
- gateway fees;
- refunds;
- refund fees;
- disputes;
- chargebacks;
- adjustments.

Each object is represented by normalized evidence rather than raw provider payload.

## Durable entities

PAY-14 adds:

- `platform_accounting_closure_runs`
- `platform_accounting_evidence`
- `platform_accounting_reconciliation_items`
- `platform_accounting_incidents`

### Closure run

A closure run is scoped by provider, environment, period and optional organization.

States:

```text
collecting
   ↓
reconciling
   ↓
review_required ──→ reconciling
   ↓
ready_to_close
   ↓
closed

collecting/reconciling/review_required/ready_to_close
   └──────────────────────────────────────────────→ failed
```

A closed period is terminal.

### Evidence

Accounting evidence is append-only.

Sides:

- `local`
- `provider`
- `settlement`

Evidence kinds include immutable local snapshots, provider API/statement evidence, provider settlement evidence, bank statements, and explicit manual attestations.

The persistence layer stores safe normalized fields plus evidence reference/hash. Raw provider or bank payloads are not stored in PAY-14 tables.

## Explicit mismatch taxonomy

The only PAY-14 mismatch categories are:

```text
provider_only
local_only
amount_mismatch
currency_mismatch
status_mismatch
settlement_missing
duplicate_provider_object
unknown_provider_object
refund_mismatch
fee_mismatch
```

No mismatch is silently converted into a money correction.

## Safe outcomes

Every reconciliation item has one of:

```text
auto_resolved_by_authoritative_evidence
retry_required
manual_review_required
security_incident
accounting_incident
```

### Auto-resolved means reconciliation-only

`auto_resolved_by_authoritative_evidence` means all required three-way evidence agrees and is authoritative.

It **does not** mean PAY-14 may change payment, refund, dispute, chargeback, fee, settlement, ledger, or invoice money state.

The database permanently enforces:

```text
automatic_financial_mutation_allowed = false
```

for every reconciliation item.

### Retry

A missing settlement after otherwise matching local/provider evidence becomes `settlement_missing → retry_required`.

Provider lag therefore does not create a false accounting correction.

### Manual review

Provider-only, local-only, unknown-provider-object, or non-authoritative evidence is routed to manual review.

### Security incident

Duplicate provider objects are treated as a security incident because they may indicate duplicate processing, replay, or provider identity corruption.

### Accounting incident

Amount, currency, status, refund, or fee mismatches are accounting incidents until reconciled with durable evidence.

## Closure rules

A period may reach `ready_to_close` only when:

- every reconciliation item is `resolved`;
- every security/accounting incident is resolved;
- durable counters match the actual item set;
- an immutable evidence manifest SHA/reference is attached.

The final `closed` transition requires the prior `ready_to_close` state plus a close actor and timestamp.

This makes closure a proof over persisted evidence, not a UI action.

## Treasury / settlement evidence

Settlement evidence can originate from:

- provider settlement reports;
- bank statement evidence;
- explicit manual attestation where supported by operations policy.

Settlement/bank evidence is never treated as permission to rewrite captured payment history.

If a captured payment is locally/provider-confirmed but settlement evidence is missing, PAY-14 records `settlement_missing` and retries rather than inventing settlement.

## Disputes and chargebacks

PAY-13 remains the authority for dispute/chargeback lifecycle and liability/loss history.

PAY-14 reconciles those facts against provider/settlement evidence but does not rewrite:

- captured payment truth;
- dispute status;
- liability/loss financial entries;
- chargeback reversal history.

Any mismatch is a reconciliation item/incident.

## Adjustments

Provider/bank adjustments are evidence objects in PAY-14. PAY-14 may reconcile or escalate them, but it does not post accounting adjustments automatically.

A real accounting adjustment must remain an explicit authorized accounting action outside PAY-14 reconciliation automation.

## Tenant isolation

Mapped tenant closure/evidence/item/incident rows use forced RLS.

Provider-wide or ownership-unresolved rows may have `organization_id = NULL`; tenant runtime cannot see those rows through the tenant policy.

Normal `app_runtime` receives read-only access only. PAY-14 introduces no production write runtime.

## Migration safety

PAY-14 is additive.

Empty lifecycle must support:

```text
PAY-13 → PAY-14 → PAY-13 → PAY-14
```

Downgrade fails closed if any closure, evidence, reconciliation item, or incident history exists.

## Certification

The exact candidate must prove:

- all eight reconciliation object classes;
- all ten mismatch categories;
- all five safe outcomes;
- authoritative three-way match auto-resolves reconciliation only;
- automated money mutation is impossible;
- append-only evidence;
- settlement-missing retry behavior;
- duplicate-provider security incident;
- amount/currency/status/refund/fee accounting incidents;
- provider-only/local-only/unknown-provider manual review;
- closure refuses unresolved retry/manual/incident state;
- closure requires durable counters and immutable evidence manifest;
- incident lifecycle and terminal closure behavior;
- forced RLS and provider-wide/null tenant isolation;
- empty migration round-trip;
- populated downgrade fail-closed;
- Platform Billing, Finance, architecture/security and migration regressions.

Gate:

`PAY14_RECONCILIATION_ACCOUNTING=PASS`
