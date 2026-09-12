# P5-W1 Core Durable-Worker Claim Fencing

Status: implementation candidate; PostgreSQL 16 runtime certification required

P5 governance base: `a246f90bb0b349d8c1f88b72e6a93733b62bc3a6`

P5 governance tree: `241792b096f22d1a7471e5d1a13a103a21d9f518`

## Scope

P5-W1 hardens the two core database queues used by:

- `app.tasks.branch_outbox_poller` over
  `public.branch_outbox_events`; and
- `app.tasks.outbox_poller` over `public.transactional_outbox`.

The certified P4 base already used bounded `FOR UPDATE SKIP LOCKED` claims,
finite database-clock leases and durable retry/dead-letter state. The P5 audit
identified two precise missing guarantees at these generic worker boundaries:

1. ownership had no monotonic claim generation independent of `leased_by`, so
   reuse of the same worker UUID could create an ABA-style stale execution; and
2. the attempt limit was applied before expired-processing reclaim, which could
   strand a worker that died during its final allowed attempt.

## Implementation contract

Migration `zg07d8e9f0a41` appends a non-negative `lease_fence bigint` to both
queues and grants only column-scoped `UPDATE (lease_fence)` to
`worker_runtime`. Because the inherited application role had table-wide INSERT
on the lifecycle outbox, the migration converts that predecessor grant to the
exact pre-P5 column set; the application can still enqueue the same rows but
cannot supply claim-fence authority. The migration adds no provider, business
state, RLS policy or broad table privilege.

Every new claim increments its attempt count and fence. Every reclaim:

- is eligible after lease expiry even when the attempt count is already at its
  maximum;
- preserves the attempt count so it cannot violate the durable bound; and
- increments the fence so the older execution is invalidated even if the same
  worker UUID is reused.

Generic completion and failure writes require all of:

- the exact durable row identity;
- `processing`/non-terminal state as applicable;
- the exact worker UUID;
- the exact claim fence; and
- an unexpired lease measured with `pg_catalog.clock_timestamp()`.

If any condition is false, the stale write changes zero rows. The worker reports
lease loss and leaves the current owner or expired row recoverable.

## Decisive P5-W1 evidence

`.github/workflows/p5w-worker-fencing-pg16.yml` must prove on one immutable SHA:

- fresh migration to exact head `zg07d8e9f0a41`;
- empty downgrade to P4 head `zf07d8e9f0a40` and re-upgrade;
- reduced-role, column-scoped claim-fence authority;
- both queues reaching their final attempt;
- lease expiry and reclaim with the same worker UUID;
- fence rotation without attempt overflow;
- rejection of stale success and stale failure writes;
- successful completion by the current fence;
- downgrade refusal after non-zero claim-generation evidence exists; and
- inherited worker and P4 contract regression.

The only success marker is:

`P5W_WORKER_FENCING_PG16=PASS`

## Explicit limitations

P5-W1 does not complete P5-W or P5. It does not yet claim:

- real worker-process death before/after commit;
- real Celery/Redis redelivery;
- propagation of the fence into every provider-specific database capability;
- provider-success/database-acknowledgement recovery;
- database restart/disconnect recovery;
- lifecycle/Finance race certification; or
- compensation crash/replay certification.

Refund-provider execution remains deferred and fail-closed. No live provider,
money movement, deployment, merge, release or tag is authorized by P5-W1.
