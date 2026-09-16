# P9-M — Production-shaped populated predecessor → HEAD rehearsal

## Baseline

P9-M starts from the P9 branch rooted at merged P8 `main` commit `8e0d6b66278e187209e6b029e11f24e475c6a24f`, tree `199b87b7dd8f65ed4c4f3303a01450ef9f8ec604`.

The exact database boundary under rehearsal is:

- populated predecessor: `zj07d8e9f0a44`
- target HEAD: `zk07d8e9f0a45`
- PostgreSQL: real PostgreSQL 16
- migration principal: reduced `migration_owner`

This slice is a forward-upgrade rehearsal. It does not authorize production deployment, release, tag creation, provider activation, live money movement, or destructive recovery work.

## Production-shaped synthetic predecessor

The decisive rehearsal database is created from migrations through `zj07d8e9f0a44` and then populated with deterministic synthetic data only. No production/customer backup or customer row may be imported.

The frozen seed shape is:

- 64 organizations;
- 3 branches per organization (192 branches total);
- 4096 durable branch-outbox rows spread across all tenants/branches;
- multiple durable event types, including `branch.lifecycle_saga`;
- pending, processed, and dead-lettered terminal states;
- fixed deterministic identifiers, timestamps, payloads, attempt counters and correlations;
- exactly 204 dead-lettered `branch.lifecycle_saga` rows, which becomes the semantic oracle for the `zk07` aggregate capability.

The seed must use only local disposable PostgreSQL state and must be reproducible from repository source.

## Predecessor capture

Before the upgrade, P9-M must prove:

1. Alembic is exactly at `zj07d8e9f0a44`.
2. `app_secure.lifecycle_saga_dead_letter_count()` does not yet exist.
3. `app_security_owner` has the already-certified bounded outbox read authority required by `zk07`.
4. `lifecycle_maintenance_runtime` has no raw `SELECT` on `public.branch_outbox_events`.
5. Deterministic fingerprints are captured for the synthetic organizations, branches, outbox rows, table ACLs, column ACLs, relation ownership and RLS/forced-RLS flags.

## Upgrade execution

The exact `zj07d8e9f0a44 → zk07d8e9f0a45` upgrade runs as reduced `migration_owner`. Duration is recorded with a monotonic clock and the Alembic output is retained as evidence.

A P9-M pass requires Alembic to report exactly `zk07d8e9f0a45` afterward. No stamping, fake revision insertion or alternate migration path is accepted.

## Data and security preservation

The stable predecessor fingerprint and post-upgrade fingerprint must compare byte-for-byte for the frozen data/security surfaces. P9-M fails if the migration changes any seeded organization, branch or outbox business row, table/column ACL, relation owner, or RLS posture.

The expected schema delta is only the `zk07` capability itself. Post-upgrade proof must establish that:

- `app_secure.lifecycle_saga_dead_letter_count()` exists;
- it is owned by `app_security_owner`;
- it is `SECURITY DEFINER` and `STABLE`;
- hardened `search_path` and `row_security=on` settings remain present;
- `PUBLIC` cannot execute it;
- only the intended lifecycle-maintenance capability receives execution among the frozen runtime roles;
- lifecycle maintenance still cannot directly select raw outbox rows;
- executing the aggregate as `lifecycle_maintenance_runtime` returns exactly `204` for the frozen synthetic predecessor.

## Inherited authority

P9-M must re-run the P9 governance contract and the relevant inherited P8/app-secure migration static contracts on the same candidate. PostgreSQL remains the durable business authority. Redis/Celery/Beat remain delivery/coordination surfaces. Refund-provider execution remains deferred and fail-closed.

## Terminal markers

A decisive P9-M runtime pass emits:

- `P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS`
- `P9M_PREDECESSOR_CAPTURE=PASS`
- `P9M_EXACT_FORWARD_UPGRADE=PASS`
- `P9M_STABLE_FINGERPRINT=PASS`
- `P9M_EXPECTED_CAPABILITY_DELTA=PASS`
- `P9M_UPGRADE_DURATION_RECORDED=PASS`
- `P9_POPULATED_PREDECESSOR_TO_HEAD=PASS`
- `P9_MIGRATION_DATA_INTEGRITY=PASS`
- `P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P9-M is complete only when those markers come from real PostgreSQL 16 evidence for one exact candidate SHA.
