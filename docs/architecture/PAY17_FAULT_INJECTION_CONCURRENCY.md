# PAY-17 — Fault Injection and Concurrency Certification

PAY-17 attacks every money-changing boundary on the exact frozen PAY-16 predecessor.

## Frozen predecessor

- PAY-16 SHA: `fd619cca8234dcdc3ff75382ce8f32bde1af4c1f`
- PAY-16 tree: `45214ab3fba96eafecbb6a3bca3ad49f8486acac`
- Alembic head remains `zz27d8e9f0a62`.
- PAY-17 adds no schema migration.
- Live provider, production credentials and live money movement remain disabled.

The authoritative fault inventory is
`docs/architecture/pay17_fault_injection_matrix_v1.json`.

## New Finance fault proofs

PAY-17 adds three Finance-specific destructive/concurrent proofs to the isolated
Finance PostgreSQL lane.

**100 concurrent callbacks.** One signed captured webhook is submitted to the
durable inbox from 100 concurrent sessions. The provider event identity must
collapse to one inbox row. One of 100 lease contenders may win. Processing the
winner must create one provider-backed payment event and converge payment state
to captured exactly once. It must create **zero** payment allocations, ledger
entries or paid-invoice transitions: verified provider evidence is not PAY-9
payment-application authority.

**Crash before financial commit.** A webhook is durably accepted and claimed,
then Finance processing is deliberately rolled back as though the process died
before commit. No payment event or allocation may survive that rollback. After
lease expiry, a new owner must reclaim with a higher fence and complete exactly
once.

**Settlement versus refund.** Settlement reconciliation and refund-intent
creation race against the same captured/applied payment from independent
sessions. Both operations lock the payment authority. The result must contain
one settlement entry and one durable requested refund obligation with one
`finance.refund.intent.created` outbox event, without over-refund or lost
obligation. No provider execution command may be created by the intent stage;
PAY-10 owns later provider-execution materialization and claiming.

## Outbound provider crash windows

PAY-17 also exercises the checkout operation fence directly.

**Death after claim but before provider call.** The provider operation claim is
committed first. If the process dies before external I/O, lease expiry converts
the ambiguous in-flight attempt to `unknown`; a reduced reconciliation identity
may then close it as provider-object-not-found. No automatic second provider POST
is allowed.

**Provider succeeds then process dies.** The fake provider returns one successful
order, then the process is treated as dead before local acknowledgement. Lease
expiry again produces `unknown`. Reconciliation binds that exact provider order
to Finance truth, and an idempotent checkout replay returns the bound order
without issuing a second provider create.

## Early webhook race repair

PAY-17 closes a real race discovered during certification.

A provider can create an order and emit a valid payment webhook before the
checkout HTTP path commits the provider order reference locally. Previously,
the provider-evidence layer classified the temporary missing payment/order
binding together with permanently invalid evidence, allowing a valid successful
payment to be dead-lettered.

PAY-17 introduces
`FinanceProviderEvidenceDeferredError` for this verified-but-not-yet-bindable
case. The webhook inbox is rolled back to its durable claim boundary, marked
retryable with `provider_binding_pending`, and later reclaimed after checkout
provider-order acknowledgement commits. Invalid signatures, amount/currency
mismatches and other permanent authority violations remain fail-closed.

The runtime proof executes the exact interleaving:

```text
local invoice/payment/provider reservation COMMIT
provider order succeeds
webhook arrives
local provider order binding absent
webhook processing => deferred/retry
checkout provider acknowledgement COMMIT
webhook reclaim with new lease
payment evidence + application + inbox completion COMMIT
```

It then proves exactly one payment event and one payment allocation.

## Lifecycle races and entitlement authority

Payment confirmation is not cancellation, renewal or expiry authority.
PAY-9 permits Finance to reconcile verified money through the canonical
subscription Finance binding, but it does not mutate product entitlement state
directly.

PAY-17 now drives three real PostgreSQL races against
`platform_subscriptions.version`. Competing payment/lifecycle mutations start
from the same expected version; exactly one may commit. A stale writer must
re-read durable truth before any retry.

- payment vs cancellation: cancellation intent is preserved even when payment
  wins the first lock race;
- payment vs renewal: convergence yields one active renewed period, never two
  overlapping/last-writer-wins periods;
- payment vs expiry: a stale expiry cannot overwrite confirmed payment, while a
  payment that loses the first race re-reads the expired version and recovers
  access from confirmed payment authority.

The Finance provider/webhook boundary still contains no direct
`platform_subscriptions` or entitlement-projection mutation. Any such authority
leak is a certification failure.

## Inherited destructive fault lanes

PAY-17 re-runs the already hardened production-shaped fault lanes on the same
exact PAY-17 SHA:

- P5-W2: real prefork SIGKILL, crash before/after commit, Redis redelivery,
  duplicate convergence and lease reclaim;
- P5-E: provider success with failed local acknowledgement/recovery;
- P5-D: Redis/broker/network and PostgreSQL disconnect/recovery;
- P5-R: real concurrent sessions, lease fencing, lifecycle/Finance visibility
  and PostgreSQL deadlock detection;
- P6-B: live worker Redis restart and reconnect;
- P6-W: worker shutdown and late-ack redelivery;
- Finance Core: provider timeout/malformed response, refund bounds, webhook
  replay/out-of-order behavior, PAY-17 callback and money races;
- PAY-10-E replayed on the PAY-17 SHA: refund worker death/reclaim, provider
  success with DB-ack loss, DB-commit/broker-ack loss, and one provider effect;
- Platform Billing isolated PG16 suite: PAY-17 payment/cancellation,
  payment/renewal and payment/expiry version races.

## Clock authority

Provider event timestamps retain PAY-16's future-skew bound. Durable provider,
webhook and refund leases use PostgreSQL `clock_timestamp()`, so caller or
worker wall clocks are not lease authority.

## Hard gate

One exact candidate must prove:

```text
PAY17_DUPLICATE_FINANCIAL_EFFECTS=0
PAY17_LOST_SUCCESSFUL_PAYMENTS=0
PAY17_LOST_REFUND_OBLIGATIONS=0
PAY17_IMPOSSIBLE_TERMINAL_STATES=0
PAY17_STUCK_UNRECOVERABLE_COMMANDS=0
PAY17_UNAUTHORIZED_ENTITLEMENTS=0
PAY17_FAULT_TOLERANCE=PASS
```

The gate does not authorize merge, release, deployment, live provider use,
production credentials or live money movement.
