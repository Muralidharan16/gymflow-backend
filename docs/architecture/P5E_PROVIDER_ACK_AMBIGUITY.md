# P5-E Provider Success / Database Acknowledgement Ambiguity

Status: P5-E runtime candidate

Certified predecessor P5-W2 SHA:
`67902b07163d7d45b0e7bda6b4cc5f46f672e8dc`

Certified predecessor P5-W2 tree:
`4ac45a1341dc00ff5179cd399091a9db37d45af8`

## Predecessor certification

P5-W2 is certified on its immutable predecessor SHA. The same candidate passed:

- P5 governance run `34735124480`, job `103665059403`;
- P5-W1 PostgreSQL 16 run `34735124481`, job `103665059641`;
- P5-W2 PostgreSQL 16 run `34735124496`, job `103665059480`;
- all 9 real worker death/redelivery/duplicate cases;
- 91 inherited P5-W1 and worker-boundary tests; and
- the `P5W2_SEPARATE_PROCESS_CRASH`, `P5W2_REAL_REDIS_REDELIVERY`, and
  `P5W2_DUPLICATE_DELIVERY` markers.

P5-E begins only from that passing checkpoint. It does not reuse any failed
P5-W2 candidate as certification evidence.

## Exact P5-E scope

P5-E proves the single frozen `provider_success_db_ack_failure` scenario on
both external-effect implementations already admitted by P4:

1. OpenSearch branch index and delete effects; and
2. Resend-style notification provider acceptance.

The downstream effect is committed first. A real PostgreSQL acknowledgement
transaction is then forced to fail with SQLSTATE `08006` before any local
provider evidence can commit. A separately started replacement worker retries
from PostgreSQL state and the durable provider store.

Refund-provider execution remains deferred and fail-closed. P5-E does not add a
refund adapter, payment call, payout, capture, ledger mutation, or other money
movement.

## Durable test-provider boundary

The P5-E worker invokes the real `OpenSearchProvider` and
`ResendEmailProvider` adapters. Their HTTP transport terminates in a
certification-only provider double whose state is a SQLite file outside
PostgreSQL. That provider store survives both worker processes and records:

- the search index/document identity, external version, desired document or
  delete tombstone, mutation-call count, and logical effect count; and
- the notification idempotency key, request hash, stable provider reference,
  send-call count, and logical effect count.

This is durable provider evidence, not an in-memory call counter. The second
worker opens the same file from a fresh Python process. Equal-version search
replay is answered through conflict plus authoritative readback; notification
replay with the same idempotency key returns the same provider reference.

The test provider does not model provider-network loss. Real dependency stop,
network interruption, Redis loss, and database restart belong to P5-D.

## Fault injection and recovery

| Surface | First provider result | Injected acknowledgement failure | Required replacement result |
|---|---|---|---|
| Search index | Desired branch document exists at the authoritative version | PostgreSQL rejects the `search_provider_ack_version` update; the acknowledgement transaction rolls back and the outbox becomes bounded `pending` retry | Same branch/index/version is replayed, provider readback proves the existing document, one local attempt/evidence row commits, and the outbox becomes `delivered` |
| Search delete | Desired delete tombstone exists at the authoritative version | PostgreSQL rejects the search acknowledgement transaction; prior local acknowledgement remains unchanged | Same branch/index/version delete is replayed, provider absence plus version is proved, one local attempt/evidence row commits, and the outbox becomes `delivered` |
| Notification acceptance | Provider stores one email under the deterministic command idempotency key | PostgreSQL rejects `processing` to `provider_accepted`; no acceptance attempt or provider reference commits locally and the live lease is preserved | Lease expiry/reclaim records the abandoned attempt as `ambiguous_outcome`, reuses the exact key, receives the same provider reference, and commits one non-terminal `provider_accepted_nonterminal` attempt |

For both search operations, two provider mutation calls produce exactly one
logical provider effect. For notification, two provider send calls produce
exactly one logical message effect. A new provider identity, branch version,
notification key, or provider reference is a hard failure.

## Provider-capability lease fence

P5-W established a monotonic `lease_fence` on durable outbox claims, but its
certified scope explicitly did not propagate that generation through every
provider-specific database capability. P5-E closes that remaining boundary.
Migration `zi07d8e9f0a43` adds fence-bearing overloads for all four search
capabilities and all seven notification capabilities. Each overload first
locks the exact outbox row and requires:

- the admitted event type;
- `processing` state;
- the exact worker UUID;
- the exact current lease fence; and
- a live lease under the PostgreSQL clock.

Only then may it delegate to the P4-certified capability body in the same
transaction. The row lock prevents the claim from rotating between fence
validation and the database effect. `worker_runtime` loses `EXECUTE` on every
legacy unfenced overload and receives only the new fence-bearing overloads;
the shared validator remains private to `app_security_owner`.

The PostgreSQL 16 runtime proves the same-worker ABA case directly: a row is
owned by one worker UUID at fence 2, and the same UUID's stale fence-1 search,
member-materialization, notification-delivery, and reconciliation capabilities
all fail with SQLSTATE `42501`. The current fence-2 search capability still
succeeds. Static contracts enumerate all 11 fenced overloads and all 11
application call sites.

## PostgreSQL and identity proof

The decisive runtime uses PostgreSQL 16 at the exact repository migration head.
The process under test binds only to a reduced `worker_runtime` member. Fixture
creation, fault-trigger control, and verification use the disposable migration
identity outside the worker process. The acknowledgement fault is a test-only
trigger installed in the disposable certification database; it does not exist
in an Alembic migration or production application path.

The tests inspect durable PostgreSQL state between the failed acknowledgement
and recovery, then again after replacement completion. PASS requires:

- no local provider acknowledgement or terminal marker from the failed
  transaction;
- a bounded `pending` search retry or a live/expired reclaimable notification
  lease;
- the same logical identity on replacement;
- exactly one downstream effect;
- exactly one committed final search evidence set, or notification ambiguity
  plus provider-acceptance evidence set; and
- no notification `succeeded`/`delivered` claim from provider acceptance alone.
- no stale or unfenced provider-specific database capability, including a
  same-worker ABA replay after lease reclaim.

## Acceptance composition

P5-E is certified only when one immutable candidate SHA passes all of:

- P5 governance and fault-matrix contracts;
- P5-W1 PostgreSQL 16 lease/fencing runtime;
- P5-W2 PostgreSQL 16 worker death/redelivery/duplicate runtime;
- P5-E static and decisive PostgreSQL 16 runtime gates; and
- inherited P4 search, notification, provider-evidence, migration, RLS, and
  process-profile boundaries.

Skipped, cancelled, timed-out, neutral, or missing decisive jobs fail the gate.
A passing P5-E checkpoint is not a PR, merge, tag, release, deployment, P5-D,
P5-R, P5-C, or P5-F authorization.
