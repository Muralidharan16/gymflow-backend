# P6 Production Readiness Acceptance Matrix

Status: P6-G frozen acceptance contract candidate

Base commit: `52949397fc3f7d4822e0c31c5ee76a21f2c182fb`

Base tree: `388a89ef09daff7c719ae07ebc5d22d152af4c58`

## Decision rule

P6 is PASS only when every decisive gate below passes on the same immutable
candidate and the terminal P6-F fan-in proves the inherited P1-P5 critical
boundaries on that same head.

The terminal hard gate is:

`P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS`

A Celery invocation may be duplicated or retried. A Redis broker process may
restart or fail over. Neither event may delete, falsely complete or permanently
strand authoritative PostgreSQL business work.

| Gate | Slice | Required decisive evidence | PASS condition |
|---|---|---|---|
| Redis production contract | P6-R | Production preflight plus real Redis configuration probe | persistence is enabled; authenticated TLS/identity validation is enforced; HA topology is declared; memory policy is `noeviction`; self-managed hosts prove `vm.overcommit_memory=1` or managed service evidence declares provider ownership |
| Broker restart/reconnect | P6-B | Real Redis stop/start with live worker and durable PostgreSQL work | worker/replacement reconnects; no false success; every pre-outage and during-outage durable obligation is eventually terminal or explicitly recoverable |
| Graceful worker SIGTERM | P6-W | Real prefork worker receives `SIGTERM` while executing a durable task | warm shutdown completes safely or leaves redeliverable/recoverable work; no partial authoritative effect and no loss |
| Late-ack/redelivery | P6-W | Real Redis + prefork worker; before-commit and after-commit/before-ack fault points | redelivery occurs when required, reuses logical identity, and converges on one durable business effect |
| Poison-message containment | P6-P | malformed/unregistered message plus repeatedly failing valid durable command | malformed work never executes; worker fleet survives; valid durable poison work reaches bounded retry/reconciliation/dead-letter disposition without hot loop or loss |
| Beat single-owner | P6-S | two scheduler contenders and ownership/failover test | at most one operational owner publishes under normal operation; stale/losing owner cannot continue publishing after ownership loss |
| Duplicate-scheduler business safety | P6-S | deliberately force duplicate periodic publication | duplicate task delivery cannot create duplicate authoritative business effects and PostgreSQL durable state converges correctly |

## Inherited gates

P6 may reuse P5 harnesses but may not replace decisive P6 evidence with historical
PASS markers. Final P6-F must re-run the critical inherited gates required to
prove:

- PostgreSQL durable authority, lease fencing and redelivery safety;
- external-effect evidence and provider-success/DB-ack ambiguity handling;
- dependency-loss and replacement-worker recovery;
- lifecycle/Finance race and compensation replay guarantees;
- RLS/runtime-identity separation and migration safety; and
- refund-provider execution remains deferred/fail-closed.

## Required P6 markers

The final same-head decision must emit all of:

- `P6_REDIS_PRODUCTION_CONTRACT=PASS`
- `P6_BROKER_RECONNECT_RECOVERY=PASS`
- `P6_WORKER_SIGTERM_SAFE=PASS`
- `P6_LATE_ACK_REDELIVERY_SAFE=PASS`
- `P6_POISON_MESSAGE_CONTAINED=PASS`
- `P6_BEAT_SINGLE_OWNER=PASS`
- `P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS`
- `P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS`
- `P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

Any missing or non-success prerequisite is a terminal P6 failure. A green unit
suite without the required real-process/real-broker evidence is insufficient.

## Forbidden acceptance shortcuts

P6 cannot be accepted by:

- observing that Redis or Celery is reachable once;
- relying on Redis persistence as the only recovery mechanism;
- counting task invocations instead of authoritative business effects;
- assuming `acks_late` implies application idempotency;
- assuming one configured Beat replica prevents accidental second ownership;
- using a DB superuser/migration identity for worker or scheduler evidence;
- mocks as the sole evidence for restart, reconnect, SIGTERM, redelivery or
  scheduler contention; or
- rewriting the candidate after decisive same-head results are collected.
