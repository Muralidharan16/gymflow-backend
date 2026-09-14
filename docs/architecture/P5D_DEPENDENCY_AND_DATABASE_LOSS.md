# P5-D — Dependency and Database Loss

## Predecessor

P5-D starts only from the P5-E certified candidate:

- commit `9e1f576dce5d36844502f1f7450e87294a4f828d`
- P5 governance run `34757688362`
- P5-W1 run `34757688264`
- P5-W2 run `34757688301`
- P5-E run `34757688342`

P5-D does not reopen the certified P5-G, P5-W1, P5-W2 or P5-E contracts.

## Scope

P5-D certifies only real dependency and database loss/rebind behavior required by the frozen P5 fault matrix.

### D1 — Redis / broker loss

The gate must stop or make unreachable a real disposable Redis broker:

1. before task delivery;
2. while a real worker is processing durable PostgreSQL work and before broker acknowledgement.

PostgreSQL is the business authority. Redis loss must not manufacture a durable success state. After Redis recovery, a replacement worker/task trigger must converge the durable job without duplicate business meaning.

### D2 — Provider network loss

The gate must use a real local HTTP provider process and the production search adapter. The provider process is stopped or unreachable during a real request. The worker must persist a bounded retryable or ambiguous disposition rather than false success. After provider restart, a replacement process must recover the same durable command and converge with one logical provider effect.

### D3 — PostgreSQL disconnect at claim

A separate CI control connection must terminate the real worker backend while `_claim_events()` is blocked inside its claim transaction. The claim must roll back: the durable job may not disappear or retain an uncommitted lease. A fresh worker connection must claim and resolve it.

### D4 — PostgreSQL disconnect during domain mutation

A canonical `active -> temporarily_closed` lifecycle transition creates Transaction-A state plus a durable `branch.lifecycle_saga`. A test-only database barrier pauses Transaction B after it starts but before commit. CI terminates that worker backend. Transaction B must roll back atomically, leaving the durable parent recoverable. After lease expiry/rebind, a replacement worker must finish Transaction B and the parent disposition without partial child effects from the killed transaction.

### D5 — PostgreSQL disconnect during provider acknowledgement

A real local provider first accepts the search mutation. A test-only database barrier then pauses the authoritative acknowledgement update. CI terminates that worker backend before acknowledgement commit. PostgreSQL must remain nonterminal and recoverable while provider state may already contain the effect. After fresh connection and replacement-process recovery, strict external versioning plus provider readback must converge to one logical effect and durable acknowledgement.

## Runtime requirements

Every destructive proof is fail-closed to a disposable local PostgreSQL 16 database and disposable local dependencies. The runtime must use reduced production-equivalent identities, FORCE RLS, real independent processes or connections, real Redis or network interruption, and `pg_terminate_backend` for database disconnects. Mock-only exceptions are insufficient.

The controller may use the local PostgreSQL superuser only as CI fault-injection infrastructure to install or remove sleep barriers, observe `pg_stat_activity`, terminate a targeted backend, and accelerate an already-owned expired lease. It must never grant BYPASSRLS or broaden application or worker privileges.

Each database-disconnect proof must establish the named boundary from `pg_stat_activity` before termination and must verify durable state through a fresh independent connection after the killed session exits.

## Bounded outcomes

Allowed recovery states are only:

- delivered or provider-acknowledged terminal success supported by durable evidence;
- pending bounded retry with `process_after` and an incremented attempt;
- provider-accepted nonterminal or reconciliation state when external acceptance is known but local completion is not;
- dead-letter after the configured finite attempt bound.

A stuck processing lease without an expiry and reclaim path, false terminal success, disappearing durable job, duplicate logical provider effect, unbounded retry, RLS bypass, broad table grant or administrative application fallback is a P5-D failure.

## Exclusions

P5-D does not certify P5-R race or deadlock interleavings, P5-C compensation crash or replay, P5-F aggregate certification, refund-provider execution, live provider credentials, PR, merge, tag, release or deployment.
