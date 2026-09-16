# P9 acceptance matrix

P9 certifies only when every marker below is emitted by same-head evidence and all inherited P1-P8 gates remain green.

| Area | Acceptance | Terminal marker |
|---|---|---|
| Governance | Exact merged P8 base/tree, Alembic predecessor/head and frozen P9 scope | `P9_GOVERNANCE_DATA_PROTECTION=PASS` |
| Populated migration | Production-shaped populated `zj07d8e9f0a44` predecessor upgrades to `zk07d8e9f0a45` on real PostgreSQL 16 | `P9_POPULATED_PREDECESSOR_TO_HEAD=PASS` |
| Migration integrity | Representative data, business invariants, RLS, ACLs, ownership and runtime-principal behavior survive | `P9_MIGRATION_DATA_INTEGRITY=PASS` |
| Lock budget | Migration lock waits are measured, bounded and fail closed on budget breach | `P9_LOCK_BUDGET=PASS` |
| Rewrite analysis | Relevant relation identity/size is captured and unexpected table rewrite is absent or explicitly reviewed | `P9_TABLE_REWRITE_ANALYSIS=PASS` |
| Migration duration | Wall-clock migration duration is recorded with environment/workload evidence | `P9_MIGRATION_DURATION_MEASURED=PASS` |
| Logical backup | Logical backup is checksummed, catalog-readable and restoreable | `P9_LOGICAL_BACKUP_INTEGRITY=PASS` |
| Physical backup | Physical PostgreSQL base backup is usable for recovery | `P9_PHYSICAL_BASE_BACKUP=PASS` |
| Full restore | Isolated restore reaches valid schema/application readiness and preserves security/business invariants | `P9_FULL_RESTORE=PASS` |
| PITR | WAL recovery proves pre-target sentinel present and post-target sentinel absent | `P9_PITR=PASS` |
| RPO | Actual recovery-point exposure is measured and within frozen target | `P9_RPO_MEASURED=PASS` |
| RTO | Recovery start to database/application readiness plus integrity validation is measured and within frozen target | `P9_RTO_MEASURED=PASS` |
| Rolling compatibility | Old and new application versions overlap safely on upgraded schema | `P9_ROLLING_SCHEMA_COMPATIBILITY=PASS` |
| App rollback | Last-known-good application can resume on upgraded schema without first destructively downgrading DB | `P9_DEPLOYMENT_ROLLBACK=PASS` |
| Bad deployment | Deliberately bad rollout is detected and recovered without stuck transitions/lost durable work | `P9_BAD_DEPLOYMENT_RECOVERY=PASS` |
| Data loss | Destructive data-loss rehearsal is recoverable within frozen RPO/RTO | `P9_DATA_LOSS_RECOVERY=PASS` |
| Inheritance | Required P1-P8 security, migration, Finance, worker, deployment and observability gates remain green | `P9_P1_P8_INHERITED=PASS` |
| Refund boundary | Provider execution remains disabled/fail-closed | `P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED` |
| Final | All inherited and P9 gates pass on one immutable SHA | `P9F_FINAL_SAME_HEAD_CERTIFICATION=PASS` |

## P9-L frozen runtime budgets

P9-L must certify the exact `zj07d8e9f0a44` → `zk07d8e9f0a45`
migration with the following unchanged limits:

- Alembic migration-session `lock_timeout`: **1,500 ms**.
- Alembic migration-session `statement_timeout`: **15,000 ms**.
- Maximum observed migration lock wait: **750 ms**.
- Maximum migration wall-clock duration: **10,000 ms**.
- Controlled `alembic_version` metadata-block hold: **200 ms**.
- Lock-observation sampling interval: **5 ms**.
- Persistent managed-role or database-role timeout settings are forbidden; the
  timeout limits are test-only connection startup settings on the exact Alembic
  session and must be re-proved by that session before HEAD migration starts.
- A granted `AccessExclusiveLock` on `organizations`, `org_branches`, or
  `branch_outbox_events` is a hard failure.
- A changed effective `relfilenode` on those relations is a table-rewrite hard
  failure; before/after relation sizes must also be recorded.

The short `alembic_version` blocker exists only to prove that real lock waiting
and blocker attribution are observable. Ordinary `AccessShareLock` and
`RowExclusiveLock` activity remains held on the populated durable outbox and
must stay compatible with the migration.

## Final hard gate

`P9_UPGRADE_AND_RECOVERY_PROVEN=PASS`

A P9 pass proves both a forward production-shaped upgrade and recovery from a bad deployment or data-loss event. No passing P9 result authorizes tag, release, production deployment, refund-provider activation or live money movement.
