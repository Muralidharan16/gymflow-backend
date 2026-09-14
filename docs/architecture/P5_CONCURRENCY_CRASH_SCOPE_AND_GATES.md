# P5 Concurrency, Crash and Durable-Worker Fault Tolerance

Status: P5 governance and fault-matrix candidate

Certified P4 merge base: `99de1600636979fa355f8ba8ff665f98dc8148bf`

Certified P4 tree: `766243d0bdd0ccd1377d57ae726222f04ff4cf1c`

## 1. Authoritative scope

P5 is the DOers hardening phase for concurrency, crash and durable-worker
fault tolerance. It is not the real-provider-adapter phase described by a
different Platform Subscription document, and it is not one of the historical
Finance Core phases that also use the number 5.

P5 must inject and prove recovery from all of the following failures:

- worker death before a database commit;
- worker death after a database commit but before Celery/broker acknowledgement;
- provider success followed by database acknowledgement failure;
- duplicate Celery task or durable-outbox delivery;
- lease expiry, reclaim and stale-worker completion attempts;
- Redis, broker or provider-network loss;
- database disconnects at claim, mutation and acknowledgement boundaries;
- deadlocks and concurrent lifecycle transitions;
- Finance and lifecycle races;
- compensation crash and replay.

The machine-readable scenario inventory is
`docs/architecture/p5_fault_injection_matrix.json`. The acceptance decision is
defined in `docs/architecture/P5_ACCEPTANCE_MATRIX.md`.

## 2. Hard gate

P5 cannot be certified if any decisive scenario permits:

1. a lost update;
2. a duplicate financial effect;
3. a stuck lifecycle or external-effect transition; or
4. an unrecoverable durable job.

For this gate, "not stuck" does not mean that a function returned. Every
non-terminal job must be durably classifiable as exactly one of:

- owned by one live, fenced lease;
- scheduled for a bounded retry;
- waiting for evidence-based reconciliation;
- durably dead-lettered with an authorized recovery path; or
- cancelled/superseded by authoritative newer state.

For this gate, "no duplicate financial effect" is measured by the logical
financial obligation and authoritative provider/accounting evidence, not by the
number of task invocations, HTTP attempts or log entries.

## 3. Inherited P4 truths

P5 inherits and may not weaken the exact tree certified and merged by Final P4.
In particular:

- a local attempt, queue acknowledgement or successful request submission is
  not downstream success;
- search success remains bound to authoritative OpenSearch evidence;
- notification provider acceptance remains non-terminal until the channel's
  authoritative terminal evidence exists;
- lifecycle refund input remains bound to authoritative Finance state and a
  deterministic logical obligation;
- refund-provider execution remains deferred and fail-closed;
- queue payloads, Redis data, webhook data, telemetry and operator input are
  never business or tenant authority;
- RLS, runtime-identity separation, least privilege and migration reversibility
  remain mandatory.

P5 may harden or add evidence around an inherited mechanism. It may not
retroactively relabel a P4 test as P5 certification without executing the
corresponding fault under the P5 protocol.

## 4. Required invariants

### 4.1 Durable authority

PostgreSQL-owned state is authoritative for claims, lifecycle state, Finance
state, idempotency identities, attempts, leases, terminal evidence and recovery
disposition. Celery and Redis are delivery mechanisms and may disappear,
redeliver or restart without changing business meaning.

### 4.2 Transaction boundaries

Each decisive test must name the commit boundary it attacks. A crash before a
commit must leave no partial durable mutation. A crash after a commit must be
safe to replay even when the broker did not observe completion.

No test may infer atomicity merely because related application statements are
inside the same Python function.

### 4.3 Lease and fencing

Claims are bounded and concurrent claimers use `FOR UPDATE SKIP LOCKED` or an
equivalent database-safe primitive. Terminal or failure writes must prove the
current ownership generation and, where time-bounded authority is required, a
live database-clock lease.

Reclaim must invalidate every older worker, including an older execution that
resumes after provider I/O or a database outage. Worker identity reuse must not
create an ABA path that lets an earlier claim commit against a later claim.

### 4.4 Idempotency and duplicate delivery

Redelivery must reuse the same logical identity derived from authoritative
state. It must not create a second command, provider object, refund, credit note,
ledger posting, notification obligation or search version for the same logical
effect.

At-least-once delivery is assumed. P5 must not claim exactly-once task
execution.

### 4.5 Ambiguous provider outcomes

If a provider may have succeeded and the database acknowledgement fails, local
state remains non-terminal until the same logical identity is recovered through
idempotent replay or provider reconciliation. Blind recreation under a new key
is forbidden.

Because refund-provider execution is still deferred, this scenario is proved
with the already admitted search and notification boundaries. P5 must not add a
live refund provider in order to satisfy the scenario.

### 4.6 Database and dependency loss

Database disconnects, Redis loss and provider-network loss must be classified
without false terminal success. Recovery must work after replacement processes
and connections start; an in-memory flag, lock or exception counter is not
durable recovery evidence.

### 4.7 Concurrent lifecycle and Finance authority

Concurrent lifecycle transitions must have one authoritative outcome and
explicit loser semantics. Lifecycle-versus-Finance races must resolve from
locked/current Finance and lifecycle state, preserve deterministic obligation
identity, and never use stale queue amounts, tenant IDs or provider references.

Deadlock or serialization victims must roll back and enter a bounded, durable
retry path. Retrying must not duplicate the winner's durable effects.

### 4.8 Compensation replay

Compensation must be restartable after process death. Replaying it must produce
one authoritative compensated state and one logical history/effect set.
Partial compensation, a frozen aggregate with no recovery disposition, or a
second compensation effect fails P5.

## 5. Decisive test protocol

Every scenario classified as decisive must:

- run against PostgreSQL 16 with the exact reduced production-equivalent
  runtime identities required by the exercised path;
- use independent processes or connections for competing workers;
- inject failure at a named claim/commit/provider/acknowledgement boundary;
- restart the affected worker, broker or database dependency where the
  scenario requires it;
- inspect durable state from a separate verification identity;
- count logical effects and financial/accounting evidence, not just task calls;
- prove stale-owner rejection and the existence of a bounded recovery route;
- emit no PASS marker until every postcondition is checked;
- run on one immutable Git SHA without source mutation during certification.

Mocks may exercise classification and rare exception paths, but cannot be the only evidence
for database atomicity, cross-process concurrency, restart
durability, lease reclaim, Redis loss or database disconnect recovery.

## 6. Controlled execution order

| Slice | Scope | Exit condition |
|---|---|---|
| P5-G | Governance, inventory and fault-injection contract | Exact scope, base, hard stops and ten required faults are mechanically guarded |
| P5-W | Core durable-worker crash, duplicate-delivery and lease/reclaim behavior | Lifecycle and transactional outboxes reject stale completion and recover before/after-commit crashes |
| P5-E | External-effect ambiguity | Search and notification recover provider-success/local-ack failure without false or duplicate success |
| P5-D | Redis/network and database loss | Replacement processes recover every affected durable job with bounded disposition |
| P5-R | Deadlocks plus lifecycle/Finance races | One authoritative lifecycle result and one logical Finance obligation survive real concurrent sessions |
| P5-C | Compensation crash/replay | Compensation is restartable, idempotent and leaves no frozen unrecoverable aggregate |
| P5-F | Final same-head certification | All P5 and inherited P1-P4 gates pass on one immutable SHA |

A later slice may begin only after the preceding slice has an immutable passing
checkpoint or an explicitly recorded scope amendment. Passing P5-G authorizes
implementation work; it does not certify runtime fault tolerance.

## 7. Hard stops and exclusions

P5 does **not** authorize:

- live-money activation, live provider credentials or production deployment;
- a refund-provider call, payment capture, payout or any other money movement;
- adding or selecting a new provider adapter;
- changing subscription entitlements or billing policy;
- arbitrary operator mutation to terminal success;
- RLS weakening, `BYPASSRLS`, broad worker grants or migration/admin credential
  fallback;
- Redis locks or Celery acknowledgement as business authority;
- unbounded retries, blanket retry of permanent failures or silent job discard;
- a tag, release, merge or deployment.

## 8. Change control

The P5-G candidate begins from the merged P4 commit and tree recorded above.
Any change to the ten fault classes, four hard-gate failures, provider/refund
boundary, runtime authority or decisive evidence protocol requires an explicit
scope amendment and invalidates prior P5-G certification evidence.
