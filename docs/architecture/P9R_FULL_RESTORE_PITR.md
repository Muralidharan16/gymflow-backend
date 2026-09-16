# P9-R — Full restore, PITR and measured recovery objectives

## Certified lineage and authority

P9-R starts from the exact certified P9-B checkpoint
`75ba68db61b244b32f6d7f1b58f4bf53b1e3e0a2` on
`hardening/p9-data-protection-disaster-recovery`. The workflow checks out only the
exact pull-request head SHA or push SHA.

The recovery database remains the real PostgreSQL 16 P9 path
`zj07d8e9f0a44` -> `zk07d8e9f0a45`, populated only with the deterministic
synthetic P9 workload. Real customer or production data is forbidden.

PostgreSQL remains durable business authority. Redis is used only as a
disposable local readiness dependency. Backup/WAL artifacts are recovery
evidence, never live business authority. Refund-provider execution remains
deferred and fail-closed.

## Frozen P9-R CI recovery objectives

These are certification budgets for the synthetic GitHub Actions rehearsal,
not production or customer SLAs:

- synthetic CI RPO target: **10,000 ms**
- synthetic CI RTO target: **120,000 ms**

RPO is the measured wall-clock exposure from the conservative recorded recovery
target boundary to the committed destructive-loss boundary. RTO is measured
with `time.monotonic_ns()` from the start of PITR reconstruction until the
recovered database has passed schema/business/security validation and the real
application `/_system/ready` endpoint succeeds.

A value above either frozen target is a hard P9-R failure.

## Full restore proof

The source database is created through the canonical reduced-role bootstrap,
provisioned with the infrastructure-owned PostgreSQL extensions, seeded at
`zj07d8e9f0a44`, and upgraded to exact `zk07d8e9f0a45`.

Before backup, P9-R creates a recovery-only synthetic sentinel table in schema
`p9r_ci`. The sentinel schema is outside the application business schema and is
excluded from `scripts/ci/p9m_stable_snapshot.sql`.

P9-R then creates one physical base backup with:

`pg_basebackup --format=plain --wal-method=stream --checkpoint=fast`

The backup manifest uses SHA-256 and must pass `pg_verifybackup`.

A separate full-restore cluster is copied from that verified base backup and
started on a private Unix socket with `listen_addresses=''`. It must prove:

- exact Alembic head `zk07d8e9f0a45`;
- byte-identical P9 stable business/security fingerprint;
- `scripts/ci/p9m_verify_head_capability.sql` passes;
- no TCP listener is enabled;
- the real FastAPI application reports `{"status":"ready"}` against the restored
  database and a disposable local Redis instance.

## Restored-environment provider isolation

Application readiness runs as a dedicated unprivileged OS identity. Its process
receives an allowlisted environment via `env -i`; notification, search and
platform-billing provider modes are explicitly disabled and no live provider
credentials are injected.

In addition, an owner-scoped firewall rule rejects all non-loopback network
egress for that recovery application identity. The workflow must prove a direct
external connection attempt from that identity is rejected before application
readiness can count. Database access stays on the private Unix socket and Redis
and OTLP evidence stay on loopback.

This is the decisive P9-R proof that the restored environment cannot
accidentally contact live providers.

## PITR destructive-boundary proof

After the verified base backup exists, the live synthetic source executes this
ordered timeline:

1. commit sentinel `pre_target`;
2. record a conservative target timestamp and create named PostgreSQL restore
   point `p9r_target`;
3. commit sentinel `post_target`;
4. commit `TRUNCATE p9r_ci.recovery_sentinels` to simulate destructive loss;
5. record the failure boundary;
6. force a WAL switch and prove the WAL segment containing the destructive
   timeline has reached the private archive.

A fresh PITR cluster is copied from the same pre-sentinel base backup, receives
`recovery.signal`, restores WAL only from the local disposable archive, and
uses:

- `recovery_target_name = 'p9r_target'`
- `recovery_target_action = 'promote'`

Certification requires:

- `pre_target` exists exactly once;
- `post_target` is absent;
- exact Alembic head remains `zk07d8e9f0a45`;
- the P9 stable business/security fingerprint matches the source baseline;
- the bounded HEAD capability proof passes;
- the recovered cluster remains private-socket only;
- the real application becomes ready against the PITR database while the
  provider-egress firewall remains active.

PITR without the explicit pre-target-present/post-target-absent proof is a hard
failure.

## Evidence

P9-R retains checksums, PostgreSQL verification logs, recovery timeline,
sentinel counts, stable fingerprints, capability output, readiness responses,
provider-isolation evidence and machine-readable RPO/RTO measurements.

Physical backup bytes and WAL segments remain ephemeral on the disposable
runner and are not uploaded as Actions artifacts.

A successful `p9r-decision.json` records at least:

- exact candidate SHA and PostgreSQL/Alembic versions;
- full restore and PITR success;
- pre/post sentinel boundary result;
- measured RPO and RTO with frozen targets;
- source/full/PITR fingerprint equality;
- full-restore and PITR application readiness;
- provider egress blocked;
- refund-provider execution `deferred_fail_closed`.

## Hard stops

P9-R fails rather than certifies if any of the following occurs:

- full restore cannot independently start or validate;
- source, full restore or PITR business/security fingerprints differ;
- the bounded HEAD capability proof fails;
- pre-target sentinel is lost or post-target sentinel survives;
- required WAL is missing;
- measured RPO exceeds 10,000 ms;
- measured RTO exceeds 120,000 ms;
- the restored application cannot reach readiness;
- the recovery application can make non-loopback provider/network connections;
- database roles or runtime privileges are broadened;
- customer/production data is introduced;
- refund-provider execution, live money movement, release or production
  deployment is enabled.

## Terminal markers

A successful exact-head P9-R run emits:

- `P9R_FULL_RESTORE_FINGERPRINT=PASS`
- `P9R_FULL_RESTORE_APP_READINESS=PASS`
- `P9R_LIVE_PROVIDER_EGRESS_BLOCKED=PASS`
- `P9R_PRE_TARGET_SURVIVES=PASS`
- `P9R_POST_TARGET_EXCLUDED=PASS`
- `P9R_PITR_APP_READINESS=PASS`
- `P9_FULL_RESTORE=PASS`
- `P9_PITR=PASS`
- `P9_RPO_MEASURED=PASS`
- `P9_RTO_MEASURED=PASS`
- `P9_DATA_LOSS_RECOVERY=PASS`
- `P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P9-R does not certify P9-C rolling compatibility, P9-D deployment rollback or
P9-F final same-head certification.
