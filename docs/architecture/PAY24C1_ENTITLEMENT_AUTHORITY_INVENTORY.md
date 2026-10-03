# PAY-24-C1 — Entitlement / Subscription Mutation Inventory and Authority Analysis

## Status

PAY-24-C1 inventory is frozen from the certified PAY-24-B application baseline:

- application SHA: `d2e7150e8e5e3df4582ed4ea8a75cd06a8f5ce9b`
- application tree: `da5a2413aa6645d6de2b8d550aee4850145e4622`
- Alembic head: `zz97d8e9f0a69`
- PAY-24-B final exact-SHA run: `37091380026`

This phase is inventory and authority analysis only. It does not authorize Stage 1,
live provider execution, deployment, production mutation, or real money movement.

```text
PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_PROVIDER_CALLS=0
PAY24_REAL_MONEY_MOVEMENT=0
```

## Required invariant

A financial/provider result is evidence, not member-entitlement authority.

```text
durable financial fact
        ↓
authorized entitlement command / durable consumer
        ↓
database-fenced entitlement mutation
        ↓
subscription/access effect exactly once
```

No browser, webhook body, provider response, operator request, or reconciliation
observation may directly activate, extend, cancel, restore, or otherwise alter a
member entitlement.

## Current canonical paid-activation path

The current certified application already has a strong paid-activation spine:

1. `MemberSubscriptionV2Service.create_subscription()` creates only a
   `pending` compatibility row and calls
   `app_secure.create_member_subscription_pending_term(...)`.
2. PAY-4 creates the canonical `subscription_terms` record as
   `pending_payment` and persists the immutable Finance binding.
3. PAY-9 turns verified captured/settled payment evidence into canonical invoice
   allocation, ledger posting, and a durable `finance.invoice.paid` outbox event.
   PAY-9 does not mutate subscription entitlement directly.
4. PAY-5 leases/fences that Finance event and calls
   `app_secure.consume_member_subscription_finance_event(...)`.
5. PAY-5 delegates the business effect to
   `app_secure.apply_member_subscription_finance_event(...)`.
6. PAY-4 revalidates the exact Finance event, binding, paid invoice,
   allocations, contributing payment states, tenant, member, plan, amount,
   currency, and entitlement window before crossing
   `pending_payment -> active|scheduled`.
7. The entitlement effect and the durable PAY-5 consumption record commit in one
   transaction. Duplicate delivery returns the existing consumption instead of
   extending entitlement again.

### Existing DB fences

- `app_runtime` has SELECT-only access to canonical lifecycle read tables,
  including `public.subscription_terms`.
- `app_runtime` has SELECT/INSERT only on
  `public.member_subscriptions_v2` and `public.subscription_members`; it has
  no UPDATE/DELETE authority there.
- PAY-4 trigger guards reject transitions to `scheduled` or `active` unless
  the call is made through the security-owner context with a
  `worker_runtime` session (or migration authority).
- PAY-5 worker consumption is lease/fence checked and idempotency journaled.

This means the current paid-admission activation path is already materially
separated from payment/provider execution.

## Inventory of entitlement-changing paths

| Path | Current classification | PAY-24-C action |
| --- | --- | --- |
| Modern subscription admission | CANONICAL_PENDING_ONLY | Preserve. No active entitlement on admission. |
| PAY-9 payment application | FINANCIAL_ONLY | Preserve. No direct subscription mutation. |
| PAY-5 Finance event dispatcher | CANONICAL_DURABLE_DELIVERY | Preserve and strengthen under PAY-24-C runtime identity/fence. |
| PAY-4 paid activation | CANONICAL_ACTIVATION_AUTHORITY | Migrate behind the single PAY-24-C entitlement authority rather than creating a second authority. |
| Canonical lifecycle reads | READ_ONLY | Preserve. |
| Stored scheduled term becoming operationally active/expired by date | DERIVED_READ_STATE | Do not create unnecessary write authority merely to mirror derived time state. |
| Legacy subscription freeze endpoint/service | LEGACY_DIRECT_MUTATION_CODE_PRESENT | Retire or route through the new command authority. |
| Legacy subscription unfreeze endpoint/service | LEGACY_DIRECT_MUTATION_CODE_PRESENT | Retire or route through the new command authority. |
| Legacy subscription cancel endpoint/service | LEGACY_DIRECT_MUTATION_CODE_PRESENT | Retire or route through the new command authority. |
| Old `app.tasks.expire_subs` task source | LEGACY_DEAD_TASK_SOURCE_PRESENT | Remove/neutralize after proving no registered route depends on it. |
| Bounded legacy expiry maintenance capability | DB_FENCED_MAINTENANCE | Preserve only as compatibility maintenance until legacy retirement is complete. |
| PAY-10 refund financial finalization | FINANCIAL_ONLY | Define an explicit durable entitlement-reversal command/event; do not let refund finalization mutate entitlement directly. |
| PAY-14 reconciliation/accounting closure | RECONCILIATION_ONLY | Preserve. Reconciliation evidence must not directly rewrite member entitlement. |
| Platform Billing organization-access resolver | SEPARATE_DOMAIN | Keep separate from member subscription entitlement authority. |

## Material gaps found by C1

### 1. Canonical activation exists, but no single all-transition authority exists yet

PAY-4/PAY-5 provide a strong paid-activation authority, but PAY-24-C must own the
complete member-entitlement transition surface: activation, renewal/extension,
cancellation, termination, freeze/resume, restoration/manual adjustment,
refund-driven reversal, expiry compatibility, and reconciliation-driven
correction commands.

A new PAY-24-C design must therefore **subsume/reuse** PAY-4/PAY-5 rather than
parallel them.

### 2. Mounted legacy interactive mutation code still exists

`app.main` mounts `app.routers.subscriptions`. That router still exposes
freeze, unfreeze, and cancel operations backed by
`SubscriptionService`, which directly assigns legacy subscription/member
state and, for unfreeze, extends `end_date`.

The modern canonical tables are DB-fenced against ordinary API UPDATE, but the
legacy route source itself is still reachable application code. C1 therefore
classifies this as a **static code bypass surface**. Its effective PostgreSQL
write authority under the exact production API login must be proven in fresh
PG16 before PAY-24-C may claim `LEGACY_BYPASS_WRITES=0`.

### 3. Legacy expiry has both a retired source path and a bounded replacement

The old `app.tasks.expire_subs` implementation contains direct ORM mutation
logic. The canonical Celery app does not schedule that legacy task and the
single-app boundary tests treat legacy task names as forbidden.

The current scheduled path is
`app.tasks.platform_maintenance.expire_legacy_member_subscriptions`, which
calls only `app_secure.expire_legacy_member_subscriptions(...)` through the
isolated `lifecycle_maintenance_runtime`. This is the compatibility path to
preserve until legacy records are retired.

### 4. Refund finalization currently stops at financial truth

PAY-10 refund finalization atomically owns refund/payment/ledger/outbox state,
but it is not member-entitlement authority. PAY-24-C must define what durable
financial fact authorizes entitlement reversal or recomputation and make that
effect deterministic/idempotent.

### 5. PAY-14 reconciliation is not entitlement authority

PAY-14 records reconciliation evidence/decisions and explicitly does not rewrite
payment/refund/dispute/invoice/ledger money truth. PAY-24-C must likewise ensure
a reconciliation correction can affect entitlement only through an explicit,
durable, authorized entitlement command.

## PAY-24-C authority target

The target is one bounded runtime capability, conceptually:

```text
finance_payment_runtime / finance_refund_runtime
        │
        │ durable facts only
        ▼
finance.outbox / durable entitlement command
        │
        ▼
entitlement_runtime
        │
        ▼
app_secure PAY-24-C command capability
        │
        ├─ validates immutable Finance/business binding
        ├─ validates transition/state-machine legality
        ├─ validates tenant/member/plan/version
        ├─ enforces idempotency/replay identity
        ├─ locks canonical entitlement row
        ├─ mutates canonical entitlement exactly once
        └─ records immutable entitlement event/command result
```

The payment, refund, reconciliation, API, worker, and maintenance identities
must not inherit or SET ROLE to `entitlement_runtime`.

## C2 implementation requirements

PAY-24-C2 must start with fresh-PG16 proof of the current privilege matrix and
then implement, in one migration lineage:

- a reduced NOLOGIN `entitlement_runtime` capability and separate deployment
  login/binding kept unbound in production;
- a durable entitlement command/result journal with immutable command identity;
- a single state-machine capability for canonical entitlement transitions;
- migration of PAY-4 paid activation through that authority without weakening
  PAY-5 durable delivery;
- deterministic refund/cancellation/reconciliation commands;
- explicit retirement/denial of legacy direct mutation paths;
- crash-before/after-mutation replay tests;
- duplicate event/command tests;
- no direct DML for API/payment/refund/reconciliation/ordinary-worker identities;
- Stage 0 remains fail-closed throughout.

## C1 gate

```ini
PAY24C1_CURRENT_STATE_INVENTORY=PASS
PAY24C1_CANONICAL_PAID_ACTIVATION_TRACED=PASS
PAY24C1_LEGACY_MUTATION_SURFACES_IDENTIFIED=PASS
PAY24C1_REFUND_AND_RECONCILIATION_BOUNDARIES_IDENTIFIED=PASS

PAY24C_ENTITLEMENT_RUNTIME_ISOLATED=NOT_YET_IMPLEMENTED
PAY24C_ENTITLEMENT_DB_FENCE=NOT_YET_IMPLEMENTED
PAY24C_IDEMPOTENT_ALL_TRANSITIONS=NOT_YET_CERTIFIED
PAY24C_LEGACY_BYPASS_WRITES=NOT_YET_ZERO
PAY24C_REFUND_REVERSAL_BINDING=NOT_YET_CERTIFIED
PAY24C_REMOTE_EXACT_SHA_CI=NOT_RUN

PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_PROVIDER_CALLS=0
PAY24_REAL_MONEY_MOVEMENT=0
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```
