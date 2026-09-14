# P6 Celery, Redis and Scheduler Production Readiness

Status: P6-G governance and production-readiness candidate

Certified P5 merge base: `52949397fc3f7d4822e0c31c5ee76a21f2c182fb`

Certified P5 tree: `388a89ef09daff7c719ae07ebc5d22d152af4c58`

P6 branch: `hardening/p6-celery-redis-scheduler-production-readiness`

## 1. Authoritative scope

P6 hardens the DOers Celery/Redis delivery plane and periodic scheduler for
production operation. It must prove that broker and worker failures recover
without losing durable business work.

P6 covers:

- Redis persistence, authenticated TLS, high-availability assumptions, memory
  policy and the Linux `vm.overcommit_memory` prerequisite;
- broker outage, restart and worker reconnect behavior;
- graceful worker `SIGTERM` behavior;
- late acknowledgement and redelivery with idempotent durable work;
- poison-message containment and bounded recovery;
- Celery Beat single-owner discipline; and
- duplicate-scheduler protection all the way to durable business effects.

The machine-readable contract is
`docs/architecture/p6_production_readiness_matrix.json`. Acceptance is defined
in `docs/architecture/P6_ACCEPTANCE_MATRIX.md`.

## 2. Hard gate

P6 cannot be certified unless every decisive scenario proves:

> Broker and worker failures recover without losing durable business work.

The hard gate is violated by any durable command, outbox item, lifecycle action,
notification obligation, search projection, maintenance obligation or Finance
obligation that disappears, becomes permanently unreachable, is falsely marked
successful, or can only be recovered from broker memory.

P6 continues to assume at-least-once delivery. It does not claim exactly-once
Celery execution. Duplicate delivery is acceptable only when authoritative
state converges on one logical business effect.

## 3. Inherited P5 truths

P6 inherits the exact tree merged after Final P5 and may not weaken it:

- PostgreSQL remains the authority for durable business work, tenant identity,
  lifecycle/Finance state, idempotency, attempts, leases and recovery status;
- Redis, Celery acknowledgement, Beat state, telemetry and queue labels are
  never business authority;
- `task_acks_late=True`, `task_reject_on_worker_lost=True` and
  `worker_prefetch_multiplier=1` are the current late-ack baseline and may not
  be weakened without an explicit scope amendment;
- stale-owner fencing, bounded retry/dead-letter behavior, provider-evidence
  rules and compensation replay guarantees remain mandatory;
- RLS, runtime identity separation and least privilege remain mandatory;
- refund-provider execution remains deferred and fail-closed; and
- no release, deployment, tag or live money movement is authorized by P6.

## 4. Redis production contract

P6 must make the Redis production assumptions explicit and machine-verifiable.
A production deployment must declare a supported topology and fail closed when
its required safety properties are absent.

### 4.1 Persistence

Redis broker persistence is resilience evidence, not business authority.
Production Redis must nevertheless use durable storage and an explicitly
validated persistence policy. For self-managed Redis, the target is AOF with
`appendfsync everysec` on durable storage, with periodic RDB snapshots retained
for recovery/backup. A managed service may provide an equivalent or stronger
persistence contract, but the deployment must attest that contract explicitly.

A Redis restart may lose broker-local delivery state within the declared
persistence window; PostgreSQL-backed recovery must still reconstruct or
redispatch every durable business obligation.

### 4.2 Authentication and TLS

Production broker and result-backend endpoints must require authentication and
encrypted transport with certificate verification. Plain unauthenticated
`redis://` is a development/test mode only. Production TLS validation may not
use `CERT_NONE` or an equivalent identity-bypass setting.

### 4.3 High availability

Production may not rely on one unreplicated Redis process as its HA contract.
The selected topology must be declared as a supported managed-HA or Sentinel
style deployment, including failover/reconnect assumptions. Failover must not
change business meaning or create a second logical effect.

### 4.4 Memory and host policy

The broker Redis memory policy must be `noeviction`; silent eviction of broker
keys is forbidden. Capacity exhaustion must surface as an operational failure
that durable PostgreSQL recovery can outlive.

For self-managed Linux Redis hosts, `vm.overcommit_memory=1` is a required
host precondition. Managed Redis must document the provider-managed equivalent
rather than pretending the application can set a host sysctl it does not own.

## 5. Celery broker/reconnect contract

P6 must explicitly configure and test startup and post-connect broker retry
behavior for the pinned Celery 5.6 line. A real Redis process must be stopped
and restarted while workers and durable work exist.

Decisive evidence must prove:

- workers do not report false task success while disconnected;
- replacement or reconnected workers resume consumption after Redis returns;
- durable PostgreSQL work present before/during the outage is redispatched or
  reconciled; and
- recovery does not require the original worker process or an in-memory flag.

## 6. Worker shutdown and late acknowledgement

`SIGTERM` is the production graceful-stop signal. Production orchestration must
not remap `SIGTERM` into Celery cold shutdown. A decisive test must send
`SIGTERM` to the main prefork worker while a real durable task is executing and
prove the warm shutdown either completes that task safely or leaves it
recoverable without duplicate business effect.

Late-ack/redelivery tests must use the real Redis broker and prefork worker.
They must attack both before-commit and after-commit/before-ack boundaries and
prove that redelivery reuses authoritative logical identity. The Redis
visibility-timeout/retry settings used in production must be explicit and
compatible with the maximum admitted task execution/recovery window.

## 7. Poison-message contract

P6 distinguishes two poison classes:

1. malformed, unsupported or unregistered broker messages that have no valid
   durable business authority; and
2. valid durable commands whose execution repeatedly fails deterministically.

Malformed/untrusted messages must not execute application code or crash the
worker fleet. Repeated application failures must not create an infinite hot
redelivery loop: attempts must be bounded and the authoritative PostgreSQL
record must end in retry, reconciliation, dead-letter/quarantine or another
explicit recoverable disposition. No business obligation may disappear merely
because a broker message is rejected.

## 8. Celery Beat single-owner and duplicate protection

Only one Beat scheduler may own the DOers schedule at a time. Embedded Beat
(`celery worker -B`) is forbidden in production. Starting a second scheduler
must either fail closed or lose a renewable operational ownership lease before
it can publish scheduled work.

The Beat ownership mechanism is operational coordination only. It may use
Redis because the production Beat identity intentionally has no database
credentials, but Redis ownership can never become business authority and must
be safe under lease loss/failover.

P6 must also deliberately run two scheduler contenders and prove a second
layer: even if duplicate periodic messages are emitted during a failover or
fault injection, downstream durable idempotency/claiming prevents duplicate
business effects.

## 9. Decisive test protocol

Every decisive P6 runtime gate must:

- run on one immutable Git SHA;
- use the exact locked Celery/Redis Python dependencies;
- use a real Redis server for broker/reconnect/redelivery/ownership scenarios;
- use PostgreSQL 16 and reduced production-equivalent runtime identities when
  durable database state is involved;
- use real worker processes for SIGTERM, crash and reconnect evidence;
- inspect authoritative durable state after restart from an independent
  verification identity where applicable;
- verify effect cardinality, not merely task invocation count;
- emit no PASS marker before all postconditions hold; and
- leave source and the checked-out Git tree unmodified during certification.

Mocks may cover parsing and rare exception classification, but mock-only proof
is forbidden for broker restart, worker shutdown, Redis failover/reconnect,
redelivery or duplicate-scheduler behavior.

## 10. Controlled execution order

| Slice | Scope | Exit condition |
|---|---|---|
| P6-G | Governance and production-readiness matrix | Exact P5 base, scope, hard gate, seven scenario classes and hard stops are mechanically frozen |
| P6-R | Redis production contract | Persistence, auth/TLS, HA, `noeviction`, host prerequisite and fail-closed preflight are proved |
| P6-B | Broker restart and reconnect | Real broker stop/start recovers durable work without false success or loss |
| P6-W | Worker lifecycle and late ack | Real prefork `SIGTERM`, before/after-commit redelivery and reconnect are safe |
| P6-P | Poison-message containment | Malformed messages are rejected safely and durable poison work reaches bounded recovery disposition |
| P6-S | Scheduler ownership | Single-owner Beat discipline and deliberate duplicate-scheduler fault produce no duplicate business effect |
| P6-F | Final same-head certification | All P6 gates plus inherited P1-P5 critical gates pass on one immutable SHA |

A later implementation slice begins only from an immutable passing predecessor
or an explicit scope amendment. P6-G authorizes P6 implementation work only;
it does not certify production readiness.

## 11. Hard stops and exclusions

P6 does **not** authorize:

- making Redis, Beat state or Celery results authoritative business storage;
- weakening PostgreSQL durable outbox/idempotency/fencing guarantees;
- broadening Beat or worker database credentials to obtain a scheduler lock;
- unauthenticated or certificate-unverified production Redis;
- production Redis key eviction as normal broker capacity management;
- unbounded retry loops or silent poison-message discard of durable work;
- embedded Beat in production workers;
- live refund-provider execution or live money movement;
- RLS weakening, `BYPASSRLS`, migration/admin credential fallback;
- merge, retarget, tag, release or deployment.

## 12. Change control

P6-G begins exactly from merged P5 commit
`52949397fc3f7d4822e0c31c5ee76a21f2c182fb` and tree
`388a89ef09daff7c719ae07ebc5d22d152af4c58`.

Changing the hard gate, Redis authority model, production TLS/HA/persistence
requirements, worker shutdown semantics, poison-message disposition, scheduler
ownership model or decisive-evidence protocol is a P6 governance amendment and
invalidates prior P6-G certification evidence.
