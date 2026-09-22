# PAY-23 — Post-Launch Financial Certification

## Purpose

PAY-23 is the final financial closure after real production payment activity.
It proves that provider money movement and DOers durable financial/product state
agree over one bounded live evidence window.

PAY-23 starts from the PAY-22 Stage 0 authorization evidence head
`6cba5c1cfa1d4296e32c91a67c46c2b73d3ab016`.

## No vacuous certification

PAY-22 currently authorizes Stage 0 only. Stage 0 has provider egress blocked and
all payment capability switches disabled. Therefore Stage 0 cannot satisfy the
post-launch gate merely because every count is zero.

The terminal marker requires:

- `environment=live`;
- PAY-22 activation Stage 1 through Stage 5;
- the active PAY-22 authorization ID and exact authorization evidence SHA;
- at least one real provider payment in the evidence window;
- at least one real provider settlement in the evidence window;
- a window between one hour and 31 days;
- at least one closed live PAY-14 accounting closure containing non-zero objects.

Until those facts exist, the PAY-23 certifier may be implemented and tested but
the enterprise payment programme is not yet classified as closed.

## Authoritative comparisons

### Provider payments == Finance payments

Provider and Finance payment count and amount-in-minor-units must agree for the
same bounded window. PAY-14 per-object reconciliation remains the authority for
reference-level matching, duplicate-provider detection, amount/currency/status
mismatch classification, and durable evidence.

### Finance allocations == invoices

PAY-23 does not incorrectly require one allocation row per invoice. Partial
payments are legitimate. Instead it proves:

- every allocation joins to its invoice;
- joined allocation count equals allocation count;
- joined allocation amount equals Finance allocation amount;
- paid/partially-paid invoice states agree with their allocations;
- no financially active invoice is orphaned.

### Finance settlements == provider settlements

Provider and Finance settlement counts and monetary totals must agree. At least
one settlement is required so this proof is not vacuous.

### Refunds == provider refunds

Refund counts and amounts must agree. A zero-refund window is valid when both
sides are zero and there are no duplicate or unexplained refund effects.

### Ledger balance

Every posted ledger entry in the certification snapshot must balance and total
posted debits must equal total posted credits.

### Subscription activations

Idempotently consumed Finance events that declare a product effect are compared
with the resulting active subscription terms. Missing effects and active
admission/renewal terms without Finance evidence are certification failures.

### Platform Billing states

At least one Platform Billing state must be inspected. Invalid durable states or
open/failed reconciliation discrepancies block closure.

## PAY-14 accounting closure reuse

PAY-23 does not create a second reconciliation system. It consumes the existing
PAY-14 durable accounting closure:

- `platform_accounting_closure_runs`;
- `platform_accounting_evidence`;
- `platform_accounting_reconciliation_items`;
- `platform_accounting_incidents`.

A qualifying closure is live, closed, non-empty, fully resolved, and has zero
mismatch, retry, manual-review, or incident count. Its expected object count must
also cover at least every provider payment, settlement, and refund included in
the PAY-23 evidence window.

## Required zero anomalies

The terminal decision requires exactly zero:

- unexplained duplicate charges;
- duplicate refunds;
- lost payments;
- orphaned invoices;
- unexplained entitlement grants;
- unresolved cross-tenant anomalies.

## Evidence handling

The local collector is
`scripts/operations/pay23_local_snapshot.sql`. It starts a
`REPEATABLE READ READ ONLY` transaction and emits aggregate-only JSON.

The final evidence manifest must be assembled from:

1. provider live payment/refund/settlement export or API evidence for the exact
   window;
2. the read-only DOers local snapshot for the same window;
3. PAY-14 closed accounting closure evidence;
4. the active PAY-22 rollout stage.

The evidence manifest must contain no raw customer identifiers and no raw provider
object identifiers. Durable provider/object-level evidence remains in the
authorized production reconciliation store; PAY-23 carries only aggregate
certification data and a SHA-256 manifest digest.

The SHA-256 is not a format-only field. It is recomputed over the UTF-8 canonical
JSON envelope (sorted keys, compact separators) excluding only the
`evidence_manifest_sha256` field. Any change to the financial totals, anomaly
counts, certification window, PAY-22 stage, or PAY-22 authorization identity
after assembly causes certification to fail.

Run the terminal certifier only on the controlled production evidence host:

```text
python scripts/operations/pay23_certify_postlaunch.py <evidence.json>
```

The script exits non-zero for any failure. It emits
`PAY23_ENTERPRISE_PAYMENT_SYSTEM=CERTIFIED` only for a complete live evidence
bundle.

## Current Stage 0 consequence

CI proves the certifier, the read-only contract, and inherited Finance/platform
regressions. CI must never emit the enterprise terminal marker from synthetic
evidence.

Because PAY-22 is currently Stage 0, PAY-23 cannot yet truthfully emit its final
programme-closure marker. A later controlled live Stage 1+ window is required.
