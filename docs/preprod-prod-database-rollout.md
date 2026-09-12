# Pre-Production and Production Database Rollout

This repository uses a deliberately split PostgreSQL identity model. Database
migrations run as the reduced `migration_owner`; ordinary API, authentication,
queue-worker, and lifecycle-maintenance workloads use separate runtime
identities. Cluster-scoped capability roles are infrastructure-owned and must
exist in the exact canonical contract before Alembic targets repository HEAD.
Alembic is intentionally not allowed to create, alter, repair, or broaden these
cluster roles.

Historical migrations that have reached a customer environment are immutable.
After deployment, fix migration defects with a new append-only corrective
revision rather than editing an already-applied revision.

## Required identity boundary

Production must have distinct credentials for all of the following:

- `DATABASE_URL`: ordinary API/application login.
- `AUTH_DATABASE_URL`: authentication/bootstrap login.
- `WORKER_DATABASE_URL`: asynchronous queue-worker login.
- `MAINTENANCE_DATABASE_URL`: lifecycle watchdog/reconciliation login.
- the migration connection used by Alembic: reduced `migration_owner`.
- a separately controlled PostgreSQL cluster-administrator connection used only
  for infrastructure bootstrap, never by the application or Alembic.

Application configuration fails closed in `ENVIRONMENT=production` if auth,
worker, or maintenance URLs are absent or reuse another production database
identity. Do not defeat that validation with shared credentials.

The production capability groups `app_runtime`, `auth_runtime`,
`worker_runtime`, and `lifecycle_maintenance_runtime` are distinct NOLOGIN,
NOINHERIT, NOBYPASSRLS identities. Runtime login identities may receive only
the deliberately declared capability edges required for their workload.
`migration_owner` remains a reduced LOGIN identity and may only SET ROLE to the
manifest-approved ownership roles `app_rls_executor` and
`app_security_owner`; it must not inherit or SET ROLE to API, auth, worker, or
maintenance capabilities.

## Canonical cluster identity source of truth

The machine-readable contract under `security/cluster_role_bootstrap/` is the
single source of truth for managed roles, exact role attributes, global role
settings, migration-owner memberships, membership options, and approved
grantors. Current CI and deployment bootstrap code must not independently encode
copies of those values.

`lifecycle_maintenance_runtime` is a first-class managed role in that contract.
There is no separate lifecycle-maintenance provisioning architecture.

`internal_billing_worker` is formally **retired**. Repository-wide P2B usage
tracing found no active application, migration, grant, ownership, or deployment
binding for that historical Platform Billing capability. It is retained only in
historical security inventory. Current clusters must not contain the role. If an
existing cluster still contains it, remove it only through the audited
infrastructure change process after proving that it owns no objects and has no
live grants; do not let application deployment or Alembic drop it implicitly.

## Fresh-cluster bootstrap

Fresh bootstrap and existing-cluster verification are deliberately separate.
The fresh-cluster bootstrap is create-only. It does not repair an unknown or
partially configured cluster.

Before the fresh-cluster bootstrap:

1. Use PostgreSQL 16 and the approved infrastructure administration channel.
2. Start from a cluster where none of the managed or retired DOERS role names
   already exist.
3. Use the manifest-approved bootstrap grantor. The current contract requires
   `current_user=session_user=postgres` with SUPERUSER so membership grantor
   provenance is deterministic and exactly matches the contract.
4. From the exact release checkout, run:

   ```bash
   DOERS_CLUSTER_ADMIN_DATABASE=postgres \
     bash scripts/release/bootstrap_cluster_roles.sh
   ```

   Supply `PGHOST`, `PGPORT`, authentication and TLS settings through the
   approved secret/configuration mechanism. The release script contains no
   password and performs no host privilege escalation.
5. Provision the real login secret for `migration_owner` through the
   infrastructure secret/identity system without changing its canonical role
   attributes, settings or memberships.
6. Provision API/auth/worker/maintenance deployment logins separately. Those
   login identities are deployment infrastructure, not canonical capability
   roles. Grant only the required capability groups with explicitly reviewed
   membership options.

The fresh bootstrap refuses an already-existing managed or retired role. This is
intentional: a partially configured cluster must go through investigation and a
reviewed infrastructure change, not an automatic `ALTER`-until-green repair.

`scripts/ci/bootstrap_cluster_roles.sh` is the CI fresh-cluster wrapper. It uses
the disposable runner's local `postgres` operating-system account and must not
be used as a production runbook command.

## Existing-cluster read-only verification

For an existing pre-production or production cluster, do **not** rerun the fresh
bootstrap. Verify the live catalog without mutation:

```bash
DOERS_CLUSTER_VERIFY_DATABASE_URL='postgresql+psycopg://migration_owner:<secret>@<host>:<port>/postgres' \
  python -s scripts/verify_cluster_role_bootstrap.py
```

This is read-only verification. It validates the exact managed role set,
attributes, settings, membership cardinality, `SET`/`INHERIT`/`ADMIN` options,
approved grantor provenance, forbidden migration-owner edges, database-specific
managed-role setting overrides, and retired-role absence. Any mismatch is a
release blocker. Correct drift through the infrastructure change process, then
rerun verification; do not add a repair mode to the verifier.

Repository CI also runs `scripts/verify_head_workflow_bootstrap.py`. It rejects
any GitHub Actions job that targets `alembic upgrade head` without invoking the
canonical external-role bootstrap first, and rejects hand-maintained copies of
canonical role creation/settings/migration membership in HEAD jobs. Intentional
historical/adversarial jobs that target predecessor revisions remain partial and
are not forced through a HEAD bootstrap.

## Infrastructure-owned extension prerequisite

Provision required PostgreSQL extensions and extension schemas with the approved
infrastructure owner before Alembic. Keep extension/schema ownership out of
runtime identities and out of `migration_owner`. The migration role may receive
only the bounded usage/execution privileges required by the lineage.

At minimum, the current PG16 production-shaped acceptance gates expect:

- `pg_partman` in infrastructure-owned schema `partman`;
- `btree_gist`;
- `citext`;
- `pg_trgm`;
- `postgis`.

Do not let an application deployment silently transfer extension ownership to
`migration_owner` or a runtime login.

## Pre-Production checklist

1. Use a fresh PostgreSQL 16 pre-production database or a controlled
   production-shaped clone, depending on the release exercise.
2. For a fresh cluster, run the canonical fresh-cluster bootstrap. For an
   existing cluster, run only the read-only verification described above.
3. Provision and verify infrastructure-owned extensions.
4. Verify the Alembic connection is `migration_owner` and that the role remains
   `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`,
   `NOREPLICATION`, and `NOBYPASSRLS`.
5. Verify API/auth/worker/maintenance logins are distinct, cannot inherit or
   `SET ROLE` into one another's capability groups, and cannot access
   `migration_owner`, `app_security_owner`, or `app_rls_executor`.
6. Record the pre-migration `alembic_version`, database catalog/security
   snapshot, live cluster-role verification result, and release commit SHA.
7. Run the exact HEAD command accepted in CI:

   ```bash
   python -s -m alembic -c alembic.ini upgrade head
   python -s -m alembic -c alembic.ini current --check-heads
   ```

8. Re-run `scripts/verify_cluster_role_bootstrap.py`. Alembic must not have
   created, altered, dropped, repaired, or gained additional access to any
   cluster capability role.
9. Confirm critical schema/security invariants, including FORCE RLS contracts,
   protected schema ownership, worker queue boundaries, lifecycle maintenance
   boundaries, and audit provenance.
10. Run the same-head hard gates: General, Platform Billing, Finance, Migration
    Lifecycle, Data Preservation, Adversarial, Worker, Branch Hours,
    Maintenance, Compensation, Architecture, P2A and P2B.
11. Start the application with distinct API/auth/worker/maintenance URLs and
    exercise real signup, login, onboarding, branch, member, membership-plan and
    admission/subscription flows.
12. Exercise lifecycle watchdog/reconciliation through the dedicated
    maintenance identity and verify API/auth/worker identities cannot perform
    that operation.
13. Review database locks, timeouts, worker retries/compensation, application
    errors, maintenance telemetry and audit integrity before approval.
14. Do not begin member-payment rollout until admission/subscription verification
    and the relevant finance hard gates pass.

## Production checklist

1. Confirm the exact release commit is the SHA accepted in pre-production and
   CI; do not deploy an unverified follow-up commit.
2. Confirm no historical migration that has previously reached production was
   edited. Production schema corrections must be new append-only revisions.
3. Take and verify a restorable backup/snapshot according to the database
   recovery policy.
4. Record current `alembic_version`, PostgreSQL version, release SHA and the
   relevant cluster-role/security inventory before changing the database.
5. Run read-only cluster verification and confirm `internal_billing_worker` is
   absent. Any drift blocks the release; do not normalize it from the deployment
   process.
6. Confirm `migration_owner` is still reduced and has no API/auth/worker/
   maintenance runtime membership or `SET ROLE` path.
7. Confirm API/auth/worker/maintenance identities remain mutually isolated.
8. Run the exact migration command accepted in pre-production:

   ```bash
   python -s -m alembic -c alembic.ini upgrade head
   python -s -m alembic -c alembic.ini current --check-heads
   ```

9. Re-run read-only cluster verification and post-migration schema/security
   invariant checks before application traffic is considered healthy.
10. Start/roll application processes with distinct `DATABASE_URL`,
    `AUTH_DATABASE_URL`, `WORKER_DATABASE_URL`, and
    `MAINTENANCE_DATABASE_URL`; never substitute `migration_owner` or the
    cluster administrator for a runtime credential.
11. Run production smoke checks for signup, login, onboarding, branch creation,
    member creation, membership-plan creation, admission/subscription creation,
    worker queue processing and lifecycle maintenance.
12. Monitor error rate, latency, PostgreSQL locks/timeouts, connection
    saturation, worker retry/compensation signals, lifecycle-maintenance
    failures and audit integrity through the deployment observation window.
13. If a release invariant fails, stop rollout and follow the incident/recovery
    procedure. Do not improvise privilege grants, disable RLS, use
    `session_replication_role = 'replica'`, or edit an already-deployed
    migration.

## Rollback and recovery policy

Production rollback is recovery-driven, not a routine `alembic downgrade`
operation. The full base→HEAD→base→HEAD lifecycle gate proves migration
reversibility for engineering acceptance, but an incident in a customer
environment may include application writes that make blind schema downgrade
unsafe.

Before deployment, choose and rehearse the recovery path appropriate to the
change: forward corrective migration, application rollback while retaining a
backward-compatible schema, or verified database restore. Record the decision,
owner and restore point in the release change record. Never downgrade past a
migration that would discard customer data unless the incident commander has an
explicitly verified restore plan and data-loss assessment.

## Test database rules

- Required variable:
  `TEST_DATABASE_URL=postgresql+asyncpg://.../<database_name_containing_test>`.
- Never set `TEST_DATABASE_URL` to a development, pre-production, or production
  application database.
- Safe cleanup may truncate only guarded disposable test databases.
- Do not use `session_replication_role = 'replica'` in tests.
- Do not hard-code destructive cleanup against tenant roots such as
  `organizations` or `owners`.
- Green tests are supporting evidence; they do not replace architecture,
  security, operational, migration, and runtime review.

## Local development recovery

If a local development database is intentionally disposable, either restore it
from a known-good local backup or reset it and rebuild through the normal
migration/application flow. Never reuse the production cluster-administrator or
migration credentials for local application runtime.
