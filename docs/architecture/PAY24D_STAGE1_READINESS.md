# PAY-24-D — Stage-1 Readiness Certification

PAY-24-D proves that Stage 1 can be provisioned safely. It does **not** authorize
Stage 1 and it does not activate any provider or money movement.

## Frozen predecessor

PAY-24-C evidence head: `8bcc2774bbbb71c9049bedb332286a424e1cbd4b`.

## Readiness boundary

The Stage-1 internal-canary worker is an isolated `entitlement_worker` process.
Its only database secret is `ENTITLEMENT_DATABASE_URL`; production startup
requires a `postgresql+asyncpg` URL with `ssl=verify-full` (or equivalent
`sslmode=verify-full`). The worker rejects AWS and Razorpay credentials and
receives no payment, refund, API, worker, maintenance or Finance-config DB
credential.

The deployment template is deliberately inert twice: it is behind the
`pay24-stage1-readiness` Compose profile **and** declares zero replicas. The
current production identity overlay still has no entitlement service and keeps
`ENTITLEMENT_DATABASE_URL` blank.

## Scheduling

The entitlement and refund-entitlement tasks remain unscheduled by Beat.
Routing is explicit only:
- entitlement apply -> `entitlement` queue;
- refund-to-entitlement durable enqueue -> ordinary `worker` queue.

A later activation phase must make an explicit reviewed scheduling/provisioning
change; PAY-24-D cannot cause command consumption merely by merging.

## Observability

`app_secure.pay24d_entitlement_readiness_snapshot()` returns aggregate counts
only: pending, processing, failed, review-required, oldest pending age and
expired processing leases. Runtime identities retain no direct SELECT on the
command journal. The entitlement worker exports those aggregate values plus its
P8 heartbeat. Telemetry failure is fail-open and cannot roll back or authorize
entitlement state.

The PAY-24-D alert pack is staged separately so Stage 0 does not page for an
intentionally absent worker. It is installed only when Stage 1 is authorized.

## Emergency rollback

The rollback order is mandatory:
1. invoke PAY-24-A begin-emergency-rollback so provider egress becomes closing;
2. disable Stage-1 scheduling/admission and scale the entitlement worker to zero;
3. do not purge queues or entitlement commands; preserve durable pending work;
4. drain/classify all provider admissions, including unknown outcomes;
5. finalize PAY-24-A rollback to Stage 0 only after admitted/active leases are zero;
6. verify egress blocked, all kill switches false, entitlement worker absent,
   command journal preserved, and no real-money/provider retry occurred.

## Zero-money internal-canary rehearsal

Before any human Stage-1 authorization, CI may create synthetic Finance and
entitlement facts in disposable PostgreSQL only. It must prove enqueue/apply,
replay, crash recovery, refund recomputation and readiness observability without
provider network I/O, live keys or real money.

## Terminal readiness posture

```text
PAY24D_STAGE1_DEPLOYMENT_TEMPLATE=PASS
PAY24D_ENTITLEMENT_TLS_SECRET_ISOLATION=PASS
PAY24D_ENTITLEMENT_OBSERVABILITY=PASS
PAY24D_EMERGENCY_ROLLBACK_CONTRACT=PASS
PAY24D_ZERO_MONEY_CANARY_REHEARSAL=PASS
PAY24_STAGE1_READINESS=PASS

PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_PROVIDER_CALLS=0
PAY24_REAL_MONEY_MOVEMENT=0
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```

## Certification evidence

PAY-24-D application code is frozen at
`e72fd20f36098b2452cf4fef12dff114e3b9faaa`
(tree `9766feea11ba3d7ec37524c688be85ee0fecfcf7`).

P2D Runtime Principal Attestation run `37105299915` completed successfully on
head `e722b8dce99287c899c92e5e0d923f8e7e4a19d7`, whose only delta after the
frozen application SHA was a runtime-test type-cast correction. The run proved
the PAY-24-D static readiness contracts, canonical external role bootstrap,
fresh PostgreSQL 16 migration through `zzb7d8e9f0a71`, exact live runtime
principal bindings, the zero-money entitlement/readiness state-machine tests,
negative principal drift rejection, and final P2B/P2C role re-verification.

This certification does not authorize Stage 1. The deployment template remains
zero-replica/profile-gated, entitlement work remains unscheduled, provider calls
remain zero, and real money movement remains zero.

