# Pre-Production and Production Database Rollout

This repository uses a deliberately split PostgreSQL identity model. Database
migrations run as the reduced `migration_owner`; ordinary API, authentication,
queue-worker, and lifecycle-maintenance workloads use separate runtime
identities. Cluster-scoped capability roles are infrastructure-owned and must be
provisioned before Alembic. Alembic is intentionally not allowed to create or
alter those cluster roles.

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
- a separately controlled cluster-administrator connection used only for
  infrastructure bootstrap, never by the application or Alembic.

Application configuration fails closed in `ENVIRONMENT=production` if auth,
worker, or maintenance URLs are absent or reuse another production database
identity. Do not defeat that validation with shared credentials.

The cluster capability `lifecycle_maintenance_runtime` is deliberately
`NOLOGIN`, `NOINHERIT`, and `NOBYPASSRLS`. The infrastructure-managed login used
by `MAINTENANCE_DATABASE_URL` may inherit that capability, but it must not inherit
API, auth, worker, migration, security-owner, or RLS-executor capabilities.
`migration_owner` must never be a member of, or be able to `SET ROLE` to,
`lifecycle_maintenance_runtime`.

## Cluster bootstrap prerequisite

Before any pre-production or production `alembic upgrade head`:

1. Provision the normal cluster roles, including `migration_owner`,
   `app_runtime`, `auth_runtime`, `worker_runtime`, `app_security_owner`, and
   `app_rls_executor`, through the infrastructure control plane.
2. Connect as a dedicated cluster administrator with `SUPERUSER` or
   `CREATEROLE`; do **not** connect as `migration_owner`.
3. From the exact release checkout, run the production-safe, idempotent
   capability provisioner using normal libpq connection settings:

   ```bash
   DOERS_CLUSTER_ADMIN_DATABASE=postgres \
     bash scripts/release/provision_lifecycle_maintenance_role.sh
   ```

   Supply `PGHOST`, `PGPORT`, `PGUSER`, authentication, and TLS settings through
   the approved secret/configuration mechanism. Do not put production passwords
   in the command line, repository, or shell history.
4. Provision the dedicated maintenance login through the infrastructure secret
   and identity system, grant it only `lifecycle_maintenance_runtime`, and use
   that login in `MAINTENANCE_DATABASE_URL`. The production boundary is the same
   as the tested role edge: `ADMIN FALSE`, inherited capability, and no ability
   to `SET ROLE` into unrelated application roles.
5. Verify the maintenance login can connect to the target database but has no
   database/schema creation privilege and no unrelated runtime-role membership.
6. Only after the cluster prerequisite is verified, authenticate separately as
   reduced `migration_owner` and run Alembic.

`scripts/ci/provision_lifecycle_maintenance_role.sh` is **CI-only**. It uses the
CI PostgreSQL superuser and intentionally fails when the capability role already
exists. Never use the CI provisioner as a production runbook command.

The production release provisioner is intentionally fail-closed. If an existing
`lifecycle_maintenance_runtime` has unsafe attributes or forbidden role edges,
it stops rather than silently repairing privilege drift. Investigate and correct
the cluster state through the infrastructure change process before retrying.

The migration lineage itself also validates this boundary and should fail if the
externally managed capability is absent or unsafe. A failure there is a release
blocker, not a reason to grant Alembic `CREATEROLE` or broader runtime access.

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

1. Use a fresh PostgreSQL 16 pre-production database or a controlled production-
   shaped clone, depending on the release exercise.
2. Verify the cluster bootstrap and infrastructure-owned extension prerequisites
   above.
3. Verify the Alembic connection is `migration_owner` and that the role remains
   `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`,
   `NOREPLICATION`, and `NOBYPASSRLS`.
4. Record the pre-migration `alembic_version`, database catalog/security
   snapshot, and release commit SHA.
5. Run:

   ```bash
   python -s -m alembic -c alembic.ini upgrade head
   python -s -m alembic -c alembic.ini current --check-heads
   ```

6. Re-verify cluster-role ownership and membership state; Alembic must not have
   created, altered, or gained access to runtime capability roles.
7. Confirm critical schema/security invariants, including:
   - required tenant tables retain ENABLE + FORCE RLS where declared by the
     migration contract;
   - `v_active_org_branches` is a view, not a table;
   - `transactional_outbox` retains its dedupe constraint/index contract;
   - branch-hours audit tables retain old/new audit data fields;
   - ordinary API runtime has no direct lifecycle-maintenance or worker queue
     capability;
   - membership-plan creation does not require direct `organizations` table
     access by ordinary runtime.
8. Run the production-shaped migration, lifecycle, worker, branch-hours,
   maintenance, finance, authorization, and application regression gates against
   the exact release SHA.
9. Start the application with distinct API/auth/worker/maintenance URLs and
   confirm production configuration validation succeeds without identity reuse.
10. Exercise the real application flow: signup, login, onboarding, branch
    creation, member creation, membership-plan creation, and
    admission/subscription creation.
11. Exercise lifecycle watchdog/reconciliation through the dedicated maintenance
    identity and verify API/auth/worker identities cannot perform that operation.
12. Exercise concurrent membership-plan creation and confirm generated plan codes
    remain unique without granting table-wide organization access.
13. Review database locks, statement timeouts, worker retries/compensation,
    application errors, and maintenance task telemetry before production approval.
14. Do not begin member-payment rollout until admission/subscription verification
    and the relevant finance hard gates pass.

## Production checklist

1. Confirm the exact release commit is the SHA accepted in pre-production and CI;
   do not deploy an unverified follow-up commit.
2. Confirm no historical migration that has previously reached production was
   edited. Production schema corrections must be new append-only revisions.
3. Take and verify a restorable backup/snapshot according to the database
   recovery policy.
4. Record current `alembic_version`, PostgreSQL version, release SHA, and the
   relevant cluster-role/security inventory before changing the database.
5. Verify the external maintenance capability, dedicated maintenance login, and
   infrastructure-owned extensions using the prerequisites above.
6. Confirm `migration_owner` is still reduced and has no API/auth/worker/
   maintenance runtime membership or `SET ROLE` path.
7. Run the exact migration command accepted in pre-production:

   ```bash
   python -s -m alembic -c alembic.ini upgrade head
   python -s -m alembic -c alembic.ini current --check-heads
   ```

8. Re-run the post-migration security/schema invariant checks before application
   traffic is considered healthy.
9. Start/roll application processes with distinct `DATABASE_URL`,
   `AUTH_DATABASE_URL`, `WORKER_DATABASE_URL`, and
   `MAINTENANCE_DATABASE_URL`; never substitute `migration_owner` or the cluster
   administrator for a runtime credential.
10. Run smoke checks for signup, login, onboarding, branch creation, member
    creation, membership-plan creation, admission/subscription creation, worker
    queue processing, and lifecycle maintenance.
11. Monitor error rate, latency, PostgreSQL locks/timeouts, connection saturation,
    worker retry/compensation signals, lifecycle-maintenance failures, and audit
    integrity through the deployment observation window.
12. If a release invariant fails, stop rollout and follow the incident/recovery
    procedure. Do not improvise privilege grants, disable RLS, use
    `session_replication_role = 'replica'`, or edit an already-deployed migration.

## Rollback and recovery policy

Production rollback is recovery-driven, not a routine `alembic downgrade`
operation. The full base→HEAD→base→HEAD lifecycle gate proves migration
reversibility for engineering acceptance, but an incident in a customer
environment may include application writes that make blind schema downgrade
unsafe.

Before deployment, choose and rehearse the recovery path appropriate to the
change: forward corrective migration, application rollback while retaining a
backward-compatible schema, or verified database restore. Record the decision,
owner, and restore point in the release change record. Never downgrade past a
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
