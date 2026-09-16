# P8-O — Production-like observability proof

P8-O is anchored to the certified P8-R checkpoint:

- parent commit: `ecec7c21aa5f70331124796d81aab18b779e64b8`
- parent tree: `8de69d66087da6fda721479a029078d102cf4437`
- branch: `hardening/p8-observability-operational-response`

## Purpose

P8-O proves that the P8 logging, metrics, alert and runbook contracts remain useful when the failure is real rather than synthesized only inside a unit test. The proof uses disposable localhost infrastructure and the already-certified P4/P5/P6 fault surfaces. Mock-only observability proof is forbidden.

The test environment uses PostgreSQL 16, a disposable `redis:7-alpine` broker/runtime dependency, real Celery prefork process death and Redis redelivery, real SQLAlchemy pool exhaustion, loopback HTTP provider timeout, real OTLP/HTTP protobuf export and deliberate OTLP sink loss. Destructive faults are refused unless the exact disposable database and explicit CI fault-enablement variables are present.

## Evidence chain

For each frozen critical failure mode, `docs/architecture/p8_production_like_observability_contract.json` binds:

1. a production-like injected scenario;
2. the bounded P8 metric that makes the failure visible;
3. the exact P8-A critical alert;
4. the exact P8-R runbook used by an operator.

The P8-O contract covers all fourteen frozen critical failure modes exactly once. The workflow additionally re-proves P8-G, P8-L, P8-M, P8-A and P8-R contracts on the same candidate head.

## Real failure surfaces

The decisive runtime proof includes the following real surfaces:

- persisted PostgreSQL search/refund operational states, including durable dead letters, Finance reconciliation mismatches and provider-ack ambiguity;
- a persisted lifecycle transition older than the fifteen-minute watchdog boundary;
- a real Redis broker queue containing stale Celery publish timestamps;
- a real SQLAlchemy QueuePool with no free checkout slot until its timeout boundary;
- a real Redis container outage and restoration;
- a real PostgreSQL service outage and restoration;
- the inherited P5-W2 Celery prefork child-death/redelivery/duplicate-convergence harness;
- the inherited P5-D provider-success / database-ack disconnect and dependency-loss harness;
- two real Beat ownership identities contending on the Redis scheduler lease;
- infrastructure-owned backup monitor ingress showing stale/failed backup evidence;
- a real ASGI request through `RequestObservabilityMiddleware` returning a slow 500 response;
- a real loopback HTTP provider timeout bridged through the bounded provider metric adapter;
- an unreachable OTLP sink while an independent durable PostgreSQL commit succeeds.

## Authority boundaries

PostgreSQL remains durable business authority. Redis, Celery and Beat remain delivery/scheduling coordination. Observability output is evidence only and cannot authorize or perform business-state transitions.

A telemetry sink outage may make the system blind, but it must not roll back, block or replace a durable business commit. Missing telemetry is therefore handled by the P8-A heartbeat-absence alert and P8-R observability-pipeline runbook, not by treating silence as healthy.

Provider-success / database-ack ambiguity never authorizes a blind external retry. Operators must preserve evidence and use the certified reconciliation/idempotency path. Refund-provider execution remains deferred and fail-closed throughout P8-O.

## Hard stops

P8-O fails if any proof targets a non-disposable database or non-local destructive dependency, if critical visibility is shown only through mocks, if telemetry mutates business state, if a provider ambiguity is resolved by blind retry, or if the slice attempts refund-provider activation, live money movement, release or deployment.

## P8-O decision markers

A successful exact-head P8-O workflow may emit only these slice markers:

- `P8_PRODUCTION_LIKE_OBSERVABILITY=PASS`
- `P8_REAL_DEPENDENCY_FAILURE_INJECTION=PASS`
- `P8_REAL_WORKER_REDELIVERY_OBSERVABILITY=PASS`
- `P8_REAL_OTLP_EXPORT=PASS`
- `P8_AUTHORITY_SURVIVES_OBSERVABILITY_FAILURE=PASS`
- `P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS`
- `P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P8-O does **not** emit `P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS`. It also does not emit the final `P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS` marker. Those are reserved for P8-F after every prerequisite is re-certified together on one immutable final head.
