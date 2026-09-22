# PAY-20 — Performance, Soak and Capacity

PAY-20 starts from frozen PAY-19 exact SHA
`39d4bcb486f797dbabcb1ea934db29db6ea8c012`, tree
`61cce864dd6208451f79b68318507802b5a53313`. PAY-20 introduces no new money authority. It adds one reversible runtime-
capacity migration, `zz47d8e9f0a64`, stacked on PAY-18/PAY-19 head
`zz37d8e9f0a63`. The migration adds no Finance table or money mutation
authority; it bounds only PAY-5 member-subscription Finance-event claiming.

## Purpose

The certification target is not raw throughput. The target is that financial
correctness remains unchanged while the system is deliberately stressed.

The required monetary surfaces are exercised directly:

```text
checkout
webhooks
payment application
ledger posting
invoice generation
subscription activation
refund creation
settlement reconciliation
```

The system-level lane separately exercises eight tenants through the hardened
production container so PAY-20 covers both Finance hot spots and cross-tenant
pressure.

## Budget discipline

PAY-20 inherits the already frozen P10 production-container budgets rather than
inventing looser limits. The committed P10 budget digest remains mandatory.
Finance-path calibration is measured on the same exact head at the same
concurrency-16 pressure as the certification load. The larger certification
sample must retain at least 60% of calibration cycle throughput and no Finance
operation may exceed 2x its same-pressure calibration p95.

In addition, every Finance operation is bounded by the frozen P10 write p95/p99
ceilings. A failing candidate may not loosen the inherited or relative budgets
to convert failure into pass.

## Finance load lane

The same-pressure calibration uses 48 complete money cycles at concurrency 16.
The certification load uses 96 complete cycles at concurrency 16. This avoids
mistaking the expected queueing effect of a 4x concurrency jump for a code
regression while preserving both the relative 2x p95 gate and frozen P10
absolute p95/p99 ceilings.

Each cycle performs:

```text
server-side checkout
  → issued numbered invoice
  → synthetic test-mode provider order
  → signed durable webhook evidence
  → provider evidence confirmation
  → payment application
  → payment-allocation ledger posting
  → settlement reconciliation
  → refund intent creation
```

No live provider network exists in the harness. The same shared synthetic
provider client must receive exactly one create-order request per fresh checkout.

A separate invoice-only burst stresses the legal-number series lock. A separate
subscription test races two consumers against the same paid Finance event and
requires exactly one activation.

## Correctness gates under load

After every Finance performance run:

- payment-event count equals completed cycles;
- PAY-9 payment-application count equals completed cycles;
- allocation count equals completed cycles;
- settlement reconciliation evidence equals completed cycles;
- refund intent count equals completed cycles;
- every posted ledger entry balances;
- provider payment references remain unique;
- legal invoice numbers remain unique;
- payment `unknown` count remains zero;
- unexpected PostgreSQL deadlocks remain zero.

Performance failure may never disable RLS, idempotency, lease fencing,
maker-checker, signature verification, accounting checks, or entitlement gates.

## Five-minute Finance soak

PAY-20 runs the complete Finance cycle continuously for at least 300 seconds.
It samples every five seconds:

```text
process RSS
PostgreSQL connections
Redis connected clients
Redis used memory
Redis rejected connections
Finance outbox backlog
oldest Finance outbox age
payment unknown count
open accounting reconciliation count
```

Webhook and settlement-reconciliation latencies are measured for every cycle.
The Finance producer soak may accumulate non-dispatchable synthetic outbox
history because not every Finance event belongs to the member-subscription
consumer. PAY-18's `finance_outbox_backlog` remains deliberately global across
all pending/processing/failed Finance outbox rows; PAY-20 must never narrow that
frozen observability definition merely to make a backlog metric smaller.

Worker capacity is therefore certified separately against the exact PAY-5
delivery obligation: bound `finance.invoice.paid` events for member
subscriptions. A synthetic 500-event burst must drain to zero eligible backlog
and zero oldest eligible age within 60 seconds, with zero retry, failed,
ack-pending, or lease-lost outcomes. Unbound Finance history remains durable and
globally observable.

## PAY-20 delivery-capacity migration

Revision `zz47d8e9f0a64` replaces only
`app_secure.claim_member_subscription_finance_events(...)`. The claim query
keeps PAY-5 lease/fence/SKIP LOCKED behavior but requires an authoritative
`finance.member_subscription_finance_bindings` match for the paid invoice.
This prevents the member-subscription worker from claiming unrelated Finance
outbox history.

The migration must round-trip `zz37 → zz47 → zz37 → zz47` on PostgreSQL 16.
Downgrade restores the previous broad PAY-5 claim semantics. The PAY-18
`app_secure.pay18_financial_observability_snapshot()` function is not replaced
by PAY-20; its global `finance_outbox_backlog` contract must remain byte-
semantically unchanged across the round trip.

## Production-container multi-tenant lane

The existing P10 production-shaped harness is reused on the PAY-20 exact head,
not historical evidence. It runs:

1. a 60-second, concurrency-24 load across eight deterministic tenants and
   enforces the frozen P10 throughput/latency/CPU/RSS budgets; and
2. after a 120-second unscored cache/JIT/database warmup at the same
   concurrency-24 workload, a full 300-second production-container
   API/worker/PostgreSQL/Redis soak whose five measured windows all retain the
   original frozen P10 budgets.

The soak hard-stops on progressive RSS growth, DB connection growth, broker
backlog, throughput degradation, latency growth or request errors.

## Inherited race/fault safety

PAY-20 retains PAY-17 concurrency semantics. Same-subscription activation,
provider/webhook races, refund races, lifecycle races and deadlock/fencing
contracts remain inherited hard gates. Load cannot turn a correctness error into
an accepted performance trade-off.

## Safety

```text
PAY20_LIVE_PROVIDER=DISABLED
PAY20_PRODUCTION_CREDENTIALS=DISABLED
PAY20_LIVE_MONEY_MOVEMENT=DISABLED
PAY20_PRODUCTION_RUNTIME_BINDING=DISABLED
PAY20_MERGE=NOT_AUTHORIZED
PAY20_RELEASE=NOT_AUTHORIZED
PAY20_DEPLOYMENT=NOT_AUTHORIZED
```

The phase closes only when one exact candidate emits:

```text
PAY20_FINANCE_CALIBRATION=PASS
PAY20_FINANCE_LOAD=PASS
PAY20_FINANCE_SOAK=PASS
PAY20_MULTI_TENANT_LOAD=PASS
PAY20_SYSTEM_SOAK=PASS
PAY20_PAYMENT_CORRECTNESS_UNDER_LOAD=PASS
PAY20_PAYMENT_PERFORMANCE=PASS
```
