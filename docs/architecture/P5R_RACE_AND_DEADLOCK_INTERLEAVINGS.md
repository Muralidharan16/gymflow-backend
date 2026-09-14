# P5-R — Race and Deadlock Interleavings

## Certified predecessor

P5-R starts only from the certified P5-D candidate and does not reopen the
certified P5-G, P5-W1, P5-W2, P5-E or P5-D contracts.

- P5-D commit: `c305b2951404e756a2bcdf9ac6c7a859bbdeba11`
- P5-D tree: `0f30a99eb3e6ad7fe11461b527bcac37d7dc3ff5`
- P5-D workflow run: `34813683351`
- P5-D workflow job: `103879856422`
- certified Alembic head: `zj07d8e9f0a44`
- P5-D static contracts: `69 passed`
- P5-D real dependency/database-loss runtime: `6 passed`
- P5-D inherited boundary reproof: `91 passed`

P5-R initially adds no schema migration. The exact database head therefore
remains `zj07d8e9f0a44`. If a real P5-R failure requires production code or
schema repair, that repair must be the smallest bounded delta and the new
immutable candidate/head must be certified again from scratch.

## Scope

P5-R certifies real PostgreSQL concurrency behavior for lifecycle, durable
worker and lifecycle-to-Finance boundaries. The gate uses independent physical
connections and real competing transactions. Mock-only concurrency is not
certification evidence.

### R1 — Same-branch API transition race

Two independent API-runtime sessions race a legal transition on the same
branch. Exactly one Transaction A may commit. The loser must receive a bounded
conflict after observing the winning state; it may not append a second history,
lifecycle saga or search command for the same old state.

The production lock order is frozen as:

1. organization advisory transaction lock;
2. branch advisory transaction lock;
3. `org_branch_state` row lock.

The pre-lock authorization/read phase may race, but the locked state must be
revalidated before mutation.

### R2 — Last-operational-branch invariant race

Two active branches in one organization race toward a non-operational status.
The organization advisory lock must serialize the organization-wide invariant.
Exactly one branch may become non-operational; the competing transition must
fail with a bounded conflict once only one operational branch remains.

This proves there is no lost update or write-skew window around the
last-operational-branch rule.

### R3 — Durable claim, reclaim and stale-fence race

Two independent worker identities race the same ready durable outbox row.
`FOR UPDATE SKIP LOCKED` must permit only one live claimant. Reclaim after lease
expiry must increment the monotonic `lease_fence`; the previous worker/fence
must be unable to mark the reclaimed row delivered.

A successful replacement owner may complete the row only while its current
lease and fence are still live.

### R4 — API versus lifecycle Transaction-B race

A leased lifecycle worker holds the branch row during Transaction B while an
API session attempts the next legal lifecycle transition. The API transaction
must block behind the row lock rather than overwrite worker state. After
Transaction B commits atomically, the API session may revalidate and continue
from the newly stable state.

No partial child commands, duplicate parent completion, impossible status or
stuck `lifecycle_transition_in_progress` state is allowed.

### R5 — Lifecycle-to-Finance handoff race

`branch.refund_required` is only a durable instruction to evaluate persisted
Finance authority. It is not provider refund authority.

The gate pauses Transaction B after the refund child has been created but
before the parent transaction commits. An independent worker/control session
must not observe the uncommitted Finance handoff. After commit, exactly one
correlated `branch.refund_required` row may become visible and claimable.

P5-R re-proves the existing P4D refund-authority static boundary. It does not
activate refund-provider execution, invent financial authority from queue
payloads, or broaden Finance table privileges.

### R6 — Deadlock detection and production lock-order proof

The disposable CI harness deliberately creates one PostgreSQL advisory-lock
deadlock using two infrastructure sessions with opposite lock order and must
observe SQLSTATE `40P01` for exactly one victim. This is a detector canary only;
it grants no application capability.

Production lifecycle contention is then required to complete without `40P01`.
The fixed organization -> branch -> row order and the organization-wide
serialization in R1/R2 are the production deadlock-avoidance evidence.

## Runtime requirements

P5-R runtime certification must use:

- disposable local PostgreSQL 16;
- the exact certified migration head;
- reduced production-equivalent auth, API and worker login identities;
- FORCE RLS and existing capability functions without BYPASSRLS;
- independent physical connections (`NullPool` for API contenders);
- real `asyncio`/thread transaction overlap, not sequential replay presented as
  concurrency;
- database barriers only as disposable CI fault-injection infrastructure;
- `pg_stat_activity` evidence for a worker/API blocking boundary where required;
- finite statement/lock/idle transaction timeouts.

The PostgreSQL superuser may be used only by CI to install/remove test barriers,
observe blocking state, and inspect committed evidence in the disposable
database. It must not grant runtime privilege or perform application work.

## Hard failures

Any of the following fails P5-R:

- two successful mutations from one stale lifecycle state;
- two branches concurrently violating the last-operational invariant;
- duplicate durable ownership of one live lease;
- stale-fence delivery succeeding after reclaim;
- API overwrite of an in-flight Transaction B;
- uncommitted lifecycle-to-Finance work becoming externally visible;
- duplicate lifecycle refund-obligation handoff for one correlation;
- production lifecycle contention surfacing SQLSTATE `40P01`;
- lost update, stuck transition, unrecoverable job or false terminal success;
- BYPASSRLS, superuser application identity, broad table grant or administrative
  application fallback.

## Exclusions

P5-R does not certify P5-C compensation crash/replay, P5-F aggregate
certification, refund-provider execution, live provider credentials, PR, merge,
tag, release or deployment. Compensation-specific crash/replay behavior remains
for P5-C even when P5-R touches the same lifecycle rows for concurrency proof.
