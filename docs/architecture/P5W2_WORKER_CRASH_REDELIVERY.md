# P5-W2 worker crash, redelivery and duplicate-delivery contract

## Frozen input

- P5-W1 base commit: `5ae630dd4239b8ce9e5345f362b8ecd3d7bf5623`
- P5-W1 base tree: `7a0d9db704f9db7c621e5297d9dcb89c176a494d`
- Frozen phase: P5 concurrency, crash and durable-worker fault tolerance
- Controlled slice: P5-W core durable-worker behavior
- Refund-provider execution remains deferred and fail-closed.

P5-W1 already established monotonic lease generations, final-attempt reclaim,
and stale success/failure/ABA rejection for both durable outboxes. P5-W2 must
retain that evidence on the same immutable candidate while proving the three
remaining P5-W scenarios below.

## Exact scope

| Frozen scenario | Injection and durable proof |
|---|---|
| Worker death before database commit | A real prefork child is killed after its database claim commits but before the selected domain transaction begins. The immediate Redis redelivery sees the live lease and cannot mutate it. The first Celery parent is then stopped; after explicit lease expiry, a separately started worker instance reclaims the row and reaches one terminal state. |
| Worker death after database commit and before broker acknowledgement | A real prefork child is killed after the production processor returns from its committed terminal transaction but before the Celery task returns and can acknowledge. Redis redelivers the same task identifier to a different child; the terminal row prevents a second logical effect. |
| Sequential and concurrent duplicate delivery | The same poller task and single durable command are delivered twice, first sequentially and then from two prefork children released through a process barrier. Exactly one claim and one terminal logical effect result. |

Both `public.branch_outbox_events` and `public.transactional_outbox` are run
through every scenario. The transactional outbox supplies a directly
countable domain effect: one `public.branch_hours_projection` row at projection
version 1. The lifecycle outbox uses a deliberately malformed saga command so
the queue can prove crash/reclaim and exactly one fail-closed terminal
disposition without executing provider, Finance, or compensation behavior.

## Decisive runtime boundary

The gate uses PostgreSQL 16, real Redis, real prefork Celery processes, and the
canonical production task entrypoints. Celery is kept at the production
semantics `task_acks_late=True`, `task_reject_on_worker_lost=True`, and
`worker_prefetch_multiplier=1`.

Fault injection is confined to
`scripts/ci/p5w2_celery_fault_hooks.py`. The CI worker loads that module only
through an explicit `--include`; no production module imports it. Its files are
one-shot crash coordination and process telemetry only. PostgreSQL remains the
sole authority for claims, terminal state, projection cardinality and lease
generation. Redis queue/unacknowledged state must drain after recovery.

The runtime gate connects with separate non-superuser, non-BYPASSRLS
`app_test_runtime` and `worker_test_runtime` identities. It refuses any host
other than loopback, any database other than `gymflow_p5w2_test`, or a broker
other than loopback Redis database 1. Destructive test enablement requires
three explicit disposable-resource acknowledgements. The pytest controller
holds the fixture identities; every spawned Celery subprocess runs the
production `worker` process profile, receives only `WORKER_DATABASE_URL`, and
must pass the production live-principal bootstep before consuming a task.

## Acceptance composition

P5-W2 evidence is valid only when the same candidate also passes:

- the frozen P5 governance contracts;
- P5-W1 real PostgreSQL stale-owner and reclaim tests;
- inherited lifecycle and transactional worker boundaries;
- the exact locked dependency comparison; and
- a clean-worktree exact-`GITHUB_SHA` marker.

The workflow emits these markers only after all checks pass:

- `P5W_PROVIDER_REFUND_EXECUTION=DEFERRED_FAIL_CLOSED`
- `P5W2_SEPARATE_PROCESS_CRASH=PASS`
- `P5W2_REAL_REDIS_REDELIVERY=PASS`
- `P5W2_DUPLICATE_DELIVERY=PASS`

No pass is claimed in this document until an immutable candidate completes the
same-head workflow.

## Explicit non-claims

This slice does not complete P5 or final P5 certification. In particular:

- provider-success/database-acknowledgement failure remains P5-E;
- Redis/network and database loss remain P5-D;
- deadlock and Finance/lifecycle races remain P5-R;
- compensation crash/replay remains P5-C;
- no live-money or refund-provider execution is enabled; and
- no PR, merge, tag, release, deployment, or protected-branch change is part of
  P5-W2 implementation.
