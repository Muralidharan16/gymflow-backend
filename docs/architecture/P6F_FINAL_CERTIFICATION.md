# P6-F Final Same-Head Certification

## Purpose

P6-F is the terminal certification gate for Phase 6 — Celery, Redis and scheduler production readiness. It does not add business behavior. It binds the completed P6 runtime slices and the inherited P1-P5 critical boundaries to one immutable Git candidate and makes a single fail-closed decision.

P6-F is PASS only when every required reusable workflow completes successfully on the same candidate. Evidence from an older SHA is not substitutable. Any candidate change invalidates the final same-head proof and requires the full P6-F fan-in to run again.

## Certified predecessor

The directly preceding P6-S candidate is:

- commit: `4170b3a3d3438294f3b7999316f55d20749894c3`
- tree: `3eff2d54ce6ea1b58cea94e8e2574430a93a9fe7`

P6-F requires that predecessor to remain an ancestor of the final candidate with the exact certified tree.

## Final topology

The final fan-in reuses the exact 27-gate inherited topology already frozen by P5-F, covering architecture, migrations, finance, P3, maintenance, P4 external effects and all P5 fault-tolerance slices. P6-F then adds six Phase 6 gates:

- P6-G — governance and frozen production-readiness contract;
- P6-R — real Redis production contract;
- P6-B — broker restart and reconnect recovery;
- P6-W — graceful worker SIGTERM and late-ack redelivery;
- P6-P — poison-message containment;
- P6-S — Beat single-owner discipline and duplicate scheduler effect safety.

The terminal P6-F job depends on the contract gate plus all 33 reusable prerequisite gates. It runs with `always()` only so it can inspect every result; any result other than `success` is a hard failure.

## Authority and safety boundaries

PostgreSQL remains the durable business authority. Redis, Celery and Beat remain delivery and coordination mechanisms only. Redis ownership, queue labels, scheduler state, worker acknowledgements and telemetry never become business authority.

P6-F does not weaken RLS, grant `BYPASSRLS`, broaden worker database authority, or give Beat database credentials. The refund provider remains deferred and fail-closed. P6-F performs no live refund provider execution and no live money movement.

## Required terminal markers

A successful terminal fan-in emits all of the following on the same immutable candidate:

```text
P6F_INHERITED_P1_P5_CRITICAL_GATES=PASS
P6_REDIS_PRODUCTION_CONTRACT=PASS
P6_BROKER_RECONNECT_RECOVERY=PASS
P6_WORKER_SIGTERM_SAFE=PASS
P6_LATE_ACK_REDELIVERY_SAFE=PASS
P6_POISON_MESSAGE_CONTAINED=PASS
P6_BEAT_SINGLE_OWNER=PASS
P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS
P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS
P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
P6F_FINAL_SAME_HEAD_CERTIFICATION=PASS
```

`P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS` is the Phase 6 hard gate. The other P6 markers remain independently required; the hard-gate marker cannot compensate for a missing or failed slice.

## Hard stops

A P6-F PASS does **not** itself perform or authorize any of the following:

- merge or retarget;
- tag or release;
- deployment;
- live refund-provider activation;
- live money movement.

After P6-F passes, a separate explicit exact-candidate merge authorization is still required. Until that authorization is given, the P6 branch remains unmerged and `main` remains unchanged.
