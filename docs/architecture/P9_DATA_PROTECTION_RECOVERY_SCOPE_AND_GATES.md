# P9 — Data protection, migration, deployment and disaster recovery

## Baseline

Phase 9 starts only from merged P8 `main` commit `8e0d6b66278e187209e6b029e11f24e475c6a24f`, tree `199b87b7dd8f65ed4c4f3303a01450ef9f8ec604`.

The current Alembic head at the P9 boundary is `zk07d8e9f0a45`; its immediate predecessor is `zj07d8e9f0a44`.

P9 does not change business authority. PostgreSQL remains the durable authority for business work. Redis, Celery and Beat remain coordination/delivery surfaces. Backup and restore artifacts are recovery evidence, not live business authority. Refund-provider execution remains deferred and fail-closed.

No production deployment, release, live-provider activation or live money movement is authorized by P9 certification work.

## Slices

- **P9-G — Governance and recovery contract.** Freeze the P9 baseline, migration/recovery authority, evidence requirements, RPO/RTO measurement definitions, hard stops and terminal markers.
- **P9-M — Production-shaped populated predecessor → HEAD rehearsal.** Build a realistic populated PostgreSQL 16 predecessor at `zj07d8e9f0a44`, migrate to `zk07d8e9f0a45`, and prove schema, row, business, RLS and ACL preservation.
- **P9-L — Lock, table-rewrite and duration analysis.** Measure migration wall-clock time, lock waits, blocked sessions, acquired lock modes, relation sizes and rewrite indicators. Unbounded or unexplained lock behavior is a certification blocker.
- **P9-B — Backup integrity.** Produce and validate a logical backup, checksum it, verify its catalog, and produce a physical base backup suitable for WAL-based recovery. A backup is not successful merely because backup creation returned zero.
- **P9-R — Full restore, PITR and measured RPO/RTO.** Restore into an isolated PostgreSQL cluster, validate business/security invariants, perform point-in-time recovery across a known destructive boundary, and record measured RPO/RTO against frozen targets.
- **P9-C — Rolling-version schema compatibility.** Prove the last-known-good application and the new application can overlap safely on the upgraded schema, and that application rollback remains possible without first destructively downgrading the database.
- **P9-D — Deployment rollback rehearsal.** Inject a bad application deployment, detect it through readiness/runtime evidence, return traffic to the last-known-good version, recover in-flight/durable work, and prove no lost updates or duplicate financial effects.
- **P9-F — Final same-head certification.** Bind all inherited P1-P8 gates and all P9 upgrade/recovery gates to one immutable SHA.

## Production-shaped migration rehearsal

The migration rehearsal must use real PostgreSQL 16 and a populated predecessor database with representative multi-tenant, lifecycle, Finance and durable-worker state. Synthetic or deliberately generated production-shaped data is acceptable. Real customer data copied into CI or an uncontrolled rehearsal environment is forbidden.

The decisive path is predecessor `zj07d8e9f0a44` → HEAD `zk07d8e9f0a45`. The rehearsal must validate more than Alembic success:

- Representative row counts and key business invariants survive.
- Tenant isolation and RLS semantics survive.
- Database roles, grants, ownership and `app_secure` boundaries survive.
- Finance uniqueness/idempotency and lifecycle invariants survive.
- The migrated database starts and serves the application under runtime principals.
- The rehearsal is repeatable from a fresh predecessor build.

Mock-only migration proof is forbidden.

## Lock, rewrite and duration contract

P9-L must produce machine-readable evidence for migration wall-clock duration, observed lock waits, blocked sessions and lock modes. It must also capture relation identity/size before and after relevant DDL so table rewrites are detected rather than guessed.

An `ACCESS EXCLUSIVE` lock is not automatically a failure—some PostgreSQL DDL legitimately requires it—but any unexpected or materially blocking `ACCESS EXCLUSIVE` lock must stop certification until its duration, blast radius and rollout strategy are explicitly reviewed.

No migration is allowed to wait indefinitely for a production lock. Rehearsal tooling must use an explicit lock budget and fail closed when that budget is exceeded.

## Backup integrity contract

P9 uses two complementary backup proofs:

1. **Logical backup** for application-level restoreability and deterministic validation. The artifact must be checksummed and its catalog must be readable before restore.
2. **Physical base backup plus WAL archive** for disaster recovery and PITR.

Backup creation alone is not success. A backup is certified only after restoreability has been demonstrated in an isolated environment.

Persisted backup artifacts must be protected according to the deployment environment's encryption/access-control policy. CI must use disposable synthetic data and must not introduce real customer data into artifacts.

## Full restore and PITR contract

A full restore must create an isolated cluster from backup and then validate:

- Alembic/schema head and critical database objects.
- Representative business rows and invariants.
- RLS, grants, owners and runtime-principal boundaries.
- Application readiness against the restored database.

The restored environment must be unable to accidentally contact live providers.

PITR must establish an unambiguous timeline:

1. Write and commit a durable **pre-target sentinel**.
2. Record the recovery target boundary.
3. Write and commit a **post-target sentinel** and then simulate destructive loss/corruption.
4. Recover to the chosen point.
5. Prove the pre-target durable record exists and the post-target record does not.

PITR without this before/after boundary proof is not accepted.

## RPO and RTO measurement

P9 does not substitute a theoretical vendor claim for measured recovery behavior.

- **RPO** is measured from the recovery target / latest recovered durable change to the failure boundary, expressed as actual data-loss exposure.
- **RTO** is measured from declared recovery start until the database and application are ready **and** integrity/security validation has completed.

Duration measurements must use a monotonic clock where applicable. The measured values and the environment/workload used to obtain them must be recorded as evidence.

Numeric RPO/RTO targets must be frozen before P9-R can certify. A final P9 pass is impossible if recovery exceeds those frozen targets.

## Rolling-version schema compatibility

P9-C must prove an expand/migrate/contract deployment discipline. During a rolling deployment:

- The old application version must remain functional on the upgraded schema.
- The new application version must be functional on the upgraded schema.
- Old and new versions must be able to overlap while sharing the same authoritative database.
- A failed new version must permit application rollback to the old version while keeping the upgraded schema in place.

A destructive contract migration that removes or changes data/schema still required by the old version is forbidden until the old version has fully drained and a later compatibility boundary explicitly permits it.

## Deployment rollback rehearsal

P9-D must rehearse a deliberately bad application rollout in production-like processes. The failure must be detected by readiness and/or runtime behavior, not by an operator pretending it failed.

Rollback must restore traffic to the last-known-good version and prove:

- The database does not require an immediate destructive downgrade as the first rollback action.
- In-flight and durable jobs are recoverable.
- No committed durable work is silently lost.
- No duplicate financial effect is created by retry/redelivery.
- Lifecycle transitions do not remain stuck.
- Post-rollback readiness and integrity checks pass.

## Recovery authority and isolation

PostgreSQL remains the durable business authority throughout P9. Redis, queues, backup catalogs and observability systems are not substitutes for authoritative committed state.

Restore/PITR environments must be isolated from production networking and live provider credentials. Recovery validation may use synthetic provider stubs or fail-closed provider configuration only.

## Hard stops

P9 must stop rather than certify if any of the following occurs:

1. Any P1-P8 security, Finance, lifecycle, worker, Redis, deployment or observability invariant is weakened.
2. Real customer data is copied into CI or an uncontrolled rehearsal environment.
3. A backup is called successful without proving restoreability.
4. PITR lacks pre-target/post-target data-boundary proof.
5. Migration lock waits are unbounded or materially blocking without explicit review.
6. A table rewrite occurs unexpectedly or without measured impact.
7. Rolling deployment requires a destructive schema change before the old version drains.
8. Application rollback requires an immediate destructive database downgrade as its first recovery step.
9. Recovery loses committed data outside the frozen RPO or exceeds the frozen RTO.
10. A restored environment can contact live providers.
11. Database credentials or runtime privileges are broadened to make rehearsal easier.
12. Refund-provider execution is enabled, live money moves, or a release/production deployment is performed under P9 certification authority.

The final gate is simple: **we can prove both a safe forward upgrade and recovery from a bad deployment or data-loss event on the exact same immutable head.**
