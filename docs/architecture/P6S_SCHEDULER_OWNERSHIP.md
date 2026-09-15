# P6-S Scheduler Ownership and Duplicate Protection

P6-S requires one active Celery Beat scheduler at a time while preserving PostgreSQL as the durable business authority. Beat retains no database credentials. Redis is used only as a renewable operational ownership lease, and downstream durable idempotency must make duplicate periodic publication harmless.

Decisive evidence must run two independent Beat processes, prove stale-owner publication stops after ownership transfer, prove takeover after owner death and lease expiry, deliberately inject duplicate periodic publication, and verify one authoritative durable PostgreSQL effect.

Required terminal markers:

- `P6_BEAT_SINGLE_OWNER=PASS`
- `P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS`
- `P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS`
- `P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

No merge, release, deployment, tag, live refund-provider execution or live money movement is authorized by this slice.
