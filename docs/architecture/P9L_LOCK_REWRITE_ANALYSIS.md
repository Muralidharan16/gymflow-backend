# P9-L lock, table-rewrite and migration-duration analysis

## Purpose

P9-L proves that the exact populated `zj07d8e9f0a44` to `zk07d8e9f0a45`
forward migration has a bounded lock profile, does not rewrite the critical
populated business relations, and completes inside a frozen wall-clock budget on
real PostgreSQL 16.

P9-L is an operational migration proof. It does not weaken the migration, bypass
RLS, disable triggers, authorize destructive downgrade, or authorize deployment.

## Exact candidate and data boundary

The workflow runs only from
`hardening/p9-data-protection-disaster-recovery` and explicitly checks out the
pull-request head SHA when triggered by PR #22. The predecessor is created with
the canonical reduced `migration_owner`, then populated by the already-certified
P9-M deterministic synthetic shape:

- 64 organizations;
- 192 branches;
- 4,096 durable branch-outbox rows;
- exactly 204 dead-lettered `branch.lifecycle_saga` rows.

No production/customer data, customer backup, production credential, or live
provider is used.

## Frozen P9-L runtime budgets

The budgets below are phase contract, not advisory values:

| Control | Frozen value |
|---|---:|
| Alembic migration-session `lock_timeout` | 1500 ms |
| Alembic migration-session `statement_timeout` | 15000 ms |
| Maximum observed migration lock wait | 750 ms |
| Maximum migration wall-clock duration | 10000 ms |
| Controlled Alembic metadata block hold | 200 ms |
| Lock sampling interval | 5 ms |

A budget increase is a governance change and requires explicit review rather
than an automatic CI relaxation.

The timeout ceilings are applied only as asyncpg connection startup settings on
the P9-L Alembic session. Persistent `ALTER ROLE`, `ALTER ROLE ... IN DATABASE`,
or other `pg_db_role_setting` changes to managed identities are forbidden. The
canonical cluster-role verifier runs unchanged, and the HEAD migration itself
must read back and prove both timeout values before it may mutate the database.
The bounded-session feature is explicitly rejected outside `ENVIRONMENT=test`.

## Concurrent-lock model

P9-L deliberately combines two different observations.

First, ordinary business-table concurrency is held for the whole migration:

- one transaction holds `AccessShareLock` on
  `public.branch_outbox_events`;
- one transaction holds `RowExclusiveLock` on
  `public.branch_outbox_events`.

These locks model concurrent read/write table activity without mutating a
business row. The `zk07` migration must still acquire its own bounded
`AccessShareLock` on that outbox and must not wait on these compatible locks.

Second, a synthetic control transaction holds `ShareLock` on
`public.alembic_version`. This is not presented as application traffic. It is a
short, known metadata blocker used only to make a real PostgreSQL lock wait and
`pg_blocking_pids()` edge observable. Once the migration is observed waiting for
its real `RowExclusiveLock` on `alembic_version`, the blocker is held for only
200 ms and then released. The Alembic-session 1500 ms `lock_timeout` remains a
second fail-closed ceiling.

This design proves the measurement path itself rather than declaring a
zero-wait migration from absence of evidence.

## Required lock evidence

The probe samples the actual `p9l_alembic_migration` backend through
`pg_catalog.pg_stat_activity`, `pg_catalog.pg_locks`, and
`pg_catalog.pg_blocking_pids()` using a monotonic clock.

Certification requires all of the following:

- at least one actual migration backend PID is observed;
- the exact migration session emits
  `P9L_ALEMBIC_SESSION_TIMEOUTS=PASS` after validating its live startup GUCs;
- the controlled `alembic_version` blocker is observed;
- the pending migration `RowExclusiveLock` on `alembic_version` is observed;
- the migration's granted `AccessShareLock` on
  `branch_outbox_events` is observed while ordinary read/write locks remain
  held;
- total and maximum contiguous lock-wait duration are measured and stay within
  750 ms;
- no granted `AccessExclusiveLock` is observed on any critical populated
  relation;
- migration wall-clock duration stays within 10000 ms.

The critical populated relations are:

- `public.organizations`;
- `public.org_branches`;
- `public.branch_outbox_events`.

Any unexpected `AccessExclusiveLock` on those relations is an immediate hard
failure.

## Table-rewrite and relation-size proof

Immediately before and after the exact forward migration, the probe records for
each critical relation:

- relation OID;
- catalog `relfilenode`;
- `pg_relation_filenode()` result;
- `pg_relation_size()`;
- `pg_total_relation_size()`;
- relation kind.

A changed effective `relfilenode` is treated as a table rewrite. A changed heap
relation size is also a P9-L failure for this migration because `zk07` is not
authorized to mutate these business tables.

The P9-M stable business/RLS/ACL/ownership fingerprint is independently captured
before and after and must remain byte-identical.

## Expected zk07 capability delta

After the lock/rewrite proof, P9-L reuses the certified P9-M capability check:
`app_secure.lifecycle_saga_dead_letter_count()` must exist with the bounded
`SECURITY DEFINER` authority while
`lifecycle_maintenance_runtime` still has no raw outbox `SELECT`.

## Terminal markers

A passing same-head P9-L run emits:

- `P9L_CONTROLLED_CONTENTION_OBSERVED=PASS`
- `P9L_ACQUIRED_LOCK_MODES_CAPTURED=PASS`
- `P9L_NO_UNEXPECTED_ACCESS_EXCLUSIVE=PASS`
- `P9L_LOCK_WAIT_BOUNDED=PASS`
- `P9_LOCK_BUDGET=PASS`
- `P9_TABLE_REWRITE_ANALYSIS=PASS`
- `P9_MIGRATION_DURATION_MEASURED=PASS`
- `P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

## Non-authorizations

P9-L does not authorize a tag, release, production deployment, production-data
copy, destructive database downgrade, refund-provider activation, or live money
movement.
