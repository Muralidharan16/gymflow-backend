# P9-B — Backup integrity and restoreability

## Certified lineage

P9-B starts only from the same P9 branch lineage that already certified P9-G,
P9-M and P9-L. The immediate certified P9-L checkpoint is
`52042bf520360c076d7277ebf99a876fb11cbd94`. The workflow itself is bound to
the exact pull-request head SHA or push SHA and must never certify a different
checkout.

The database boundary remains the exact production-shaped PostgreSQL 16 path
`zj07d8e9f0a44` -> `zk07d8e9f0a45`. Only deterministic synthetic P9 data is
permitted. Real customer data is forbidden.

P9-B proves backup integrity and basic restoreability. It does **not** certify
PITR, RPO, RTO, deployment rollback, release, production deployment, provider
activation, or money movement; those remain later P9 gates.

## Logical backup proof

The source database is built on real PostgreSQL 16, seeded with the existing
P9-M deterministic production-shaped workload, and upgraded to exact
`zk07d8e9f0a45` before backup.

The logical backup contract is frozen as follows:

- `pg_dump --format=custom` is used so the archive has a PostgreSQL catalog.
- The archive is hashed with SHA-256 and its byte size is retained as evidence.
- `pg_restore --list` must read the complete archive catalog before restore.
- A deliberately truncated copy must be rejected by `pg_restore --list`; a
  parser that accepts the corrupted archive is a hard failure.
- The intact archive is restored as the infrastructure recovery operator into
  a fresh disposable database. Object ownership and ACLs are not stripped.
- The restored Alembic version must be exactly `zk07d8e9f0a45`.
- `scripts/ci/p9m_stable_snapshot.sql` must be byte-identical between source and
  logical restore.
- `scripts/ci/p9m_verify_head_capability.sql` must pass on the restore, proving
  the aggregate lifecycle capability, SECURITY DEFINER owner/configuration and
  blocked raw-table/runtime privileges survived.

The logical restore database is isolated from live providers and contains only
synthetic CI data.

## Physical base-backup and WAL proof

The disposable PostgreSQL 16 source cluster enables WAL archiving to a local,
runner-owned recovery directory before the source database is built. P9-B must
force a WAL switch and prove at least one archived WAL segment exists.

The physical backup contract is frozen as follows:

- `pg_basebackup --format=plain --wal-method=stream --checkpoint=fast` is used.
- The backup manifest uses SHA-256 checksums.
- `pg_verifybackup` must validate the complete base backup.
- The SHA-256 digest of `backup_manifest`, physical backup byte size, WAL
  archive inventory and WAL SHA-256 digests are retained as evidence.
- The verified backup is copied into a separate disposable data directory and
  booted as a second PostgreSQL instance.
- The cloned instance listens on a private Unix socket with TCP
  `listen_addresses=''`; source archiving is disabled on the clone.
- The cloned database must report exact Alembic head `zk07d8e9f0a45`.
- The source and physical-clone stable snapshots must be byte-identical.
- The exact P9-M HEAD capability proof must pass on the physical clone.

Starting the cloned base backup is the P9-B usability proof for the physical
artifact. P9-R remains responsible for a full disaster-recovery/PITR rehearsal
with pre-target/post-target sentinels and frozen measured RPO/RTO targets.

## Evidence retention

Backup bytes remain ephemeral inside the disposable CI runner. The retained
GitHub Actions artifact contains checksums, manifests/inventory, catalogs,
fingerprints and validation logs rather than customer or production data.
This keeps the evidence auditable without turning CI artifacts into a
production backup store.

## Hard stops

P9-B fails closed if any of the following occurs:

- backup creation succeeds but catalog validation or restoreability does not;
- the corrupted logical archive is accepted;
- source and restored fingerprints differ;
- exact Alembic head or the bounded `app_secure` capability differs;
- `pg_verifybackup` fails;
- no WAL archive evidence exists;
- the physical clone cannot start independently;
- the physical clone exposes TCP networking;
- role/runtime privileges are broadened to make restore easier;
- real customer/production data is introduced;
- refund-provider execution, release, production deployment or live money
  movement is enabled.

## Terminal markers

A successful exact-head P9-B run emits all of:

- `P9B_LOGICAL_CATALOG_READABLE=PASS`
- `P9B_CORRUPT_LOGICAL_BACKUP_REJECTED=PASS`
- `P9B_LOGICAL_RESTORE_FINGERPRINT=PASS`
- `P9_LOGICAL_BACKUP_INTEGRITY=PASS`
- `P9B_WAL_ARCHIVE=PASS`
- `P9B_PHYSICAL_MANIFEST_VERIFIED=PASS`
- `P9B_PHYSICAL_CLONE_STARTUP=PASS`
- `P9B_PHYSICAL_RESTORE_FINGERPRINT=PASS`
- `P9_PHYSICAL_BASE_BACKUP=PASS`
- `P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`
