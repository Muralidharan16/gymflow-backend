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
`auth_p5w2_runtime`, `app_test_runtime`, and `worker_test_runtime` identities.
The auth identity creates the canonical initial active/primary branch-state row
through the certified P3A bootstrap policy; the application identity enqueues
the event. The gate refuses any host other than loopback, any database other
than `gymflow_p5w2_test`, or a broker other than loopback Redis database 1.
Destructive test enablement requires three explicit disposable-resource
acknowledgements. The pytest controller holds the fixture identities; every
spawned Celery subprocess runs the production `worker` process profile,
receives only `WORKER_DATABASE_URL`, and must pass the production live-principal
bootstep before consuming a task.

## Failed-candidate provenance

Candidate `bf0a9119e70fb3abd7415ff1601a3a64c861e67d` is not P5-W2 certified.
GitHub Actions run `34702421005`, job `103576425030`, failed the first real
transactional recovery case after the replacement worker reclaimed the event.
The production projection correctly raised `LookupError` because the disposable
fixture created the branch root but omitted its required
`public.org_branch_state` row. Redis was healthy, the prefork worker started,
the task was delivered, and the durable row reached retry disposition.

The first repair is confined to the test boundary: it provisions a reduced
`auth_p5w2_runtime` login and creates the canonical initial branch state through
the existing auth policy before the application identity enqueues work. It does
not change the production projection query, RLS policy, worker code, lease
semantics, provider boundary, or Finance behavior. A new immutable candidate
must execute the full same-head gate; the failed SHA is not reused.

Candidate `47ab37d7e156739bbe73d05b60f97d9ea9c3b71f` is also not P5-W2
certified. Governance and the P5-W1 same-head reproof passed, but GitHub Actions
run `34703762190`, job `103579985787`, reached the production projection upsert
and PostgreSQL denied the worker access to `public.organization_members`.

This exposed a production policy-audience defect. The legacy
`tenant_isolation_projection` `FOR ALL` policy still applied to PUBLIC, so its
application-principal membership predicate could be evaluated for the worker
alongside the dedicated lease-bound worker policies. The repair changes only
that legacy policy audience from PUBLIC to `app_runtime`, preserving its exact
predicate. It neither grants the worker access to `organization_members` nor
weakens FORCE RLS, lease checks, destructive-privilege restrictions, or tenant
lineage. The migration is reversible to the exact predecessor audience and is
exercised through an empty PostgreSQL 16 downgrade/upgrade lifecycle before the
real worker crash matrix.

Candidate `2853b8ff18207c834945c5681ec6c0cc5cce66b1` is also not P5-W2
certified. Governance and P5-W1 passed, and GitHub Actions run `34732854711`,
job `103658769506`, completed the exact PostgreSQL 16 migration
upgrade/downgrade/upgrade lifecycle. The gate then stopped in its initial
negative-access assertion, before any crash case ran: the worker probe queried
`organization_members` without first establishing the required fail-closed
`app.current_org_id` context for that table, so PostgreSQL raised the expected
missing-context error before reaching the asserted table-privilege denial.

This is a test-boundary repair only. The negative-access probe now sets a random
transaction-local organization context before asserting that the reduced worker
login still receives `InsufficientPrivilege`. It does not change the migration,
production policy, worker grants, worker code, lease semantics, provider
boundary, or Finance behavior. The failed SHA is not reused.

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
