# P3E-A Background Execution Identity Inventory

Status: **P3E-A discovery baseline — not certification**

Frozen parent: `3c3e446bbed62a452f4459397c25c5e55543a205` (certified P3D)

P3E principle:

> A queue message, scheduler entry, Redis value, task argument, or stale job row is data. It is never authorization by itself.

This inventory records the background execution surfaces present at the frozen P3D
parent before P3E changes their production authority. P3D remains immutable. P3E
does not assume that a database migration is required; runtime and PostgreSQL
evidence must justify any privilege change.

## Process identity baseline

The inherited process boundary is already strong and is a P3E invariant:

- API work uses the API runtime database binding.
- ordinary Celery work uses the worker database binding.
- lifecycle/platform global maintenance uses the isolated maintenance binding.
- production Celery startup attests the configured live PostgreSQL identity before
  broker consumption.
- worker and maintenance async engines use `NullPool`.
- legacy synchronous worker tasks use the dedicated worker Psycopg engine, not the
  API compatibility engine.
- maintenance tasks install transaction-local maintenance context and call bounded
  database capabilities rather than receiving broad direct table grants.

P3E must preserve these properties while hardening task-level authority.

## Inventory and disposition

| Surface | Trigger / producer | Intended scope | Execution identity | Current authority source | Durability / concurrency | P3E-A disposition |
|---|---|---|---|---|---|---|
| General outbox poller | Celery Beat | tenant-bound events | worker | durable outbox row + tenant/worker lease context | `SKIP LOCKED`, lease ownership, completion/release checks | **REFERENCE / preserve** |
| Branch operating-hours outbox | Celery Beat | tenant + branch | worker | durable event + tenant/worker lease | durable lease/fencing and child-command boundary | **REFERENCE / preserve** |
| Branch lifecycle watchdog/reconciliation | Celery Beat | cross-tenant maintenance | maintenance | maintenance DB identity + transaction-local `lifecycle` context | DB claims / `SKIP LOCKED` where reconciliation applies | **REFERENCE / preserve** |
| Platform idempotency/cache/geocoding sweeps | Celery Beat | cross-tenant maintenance | maintenance | maintenance DB identity + transaction-local `platform` context + bounded `app_secure` functions | bounded batches, `SKIP LOCKED` where claiming applies | **REFERENCE / preserve** |
| Tenant geocoding execution | maintenance dispatch / task producer | one tenant/address | worker | task locators are rechecked against tenant-visible current DB state | retry state exists; not the same explicit lease/fencing model as durable outboxes | **PARTIAL — certify duplicate/stale execution semantics** |
| Logo/cover image processing | authorized asset confirm route | one organization/upload | worker sync | authenticated producer supplies `org_id`, `upload_id`, `user_id`; worker later trusts task values/S3 key shape | Celery retry, no durable upload-authority record found | **PARTIAL — execution-time authority gap candidate** |
| Old asset deletion | image pipeline | object keys | worker / S3 | arbitrary key list from task message | no durable DB-backed delete claim found | **GAP CANDIDATE — constrain S3 deletion authority** |
| Orphan asset cleanup | Celery Beat | global object-store sweep | worker sync | global organization-key scan + S3 listing | no isolated maintenance classification | **GAP — reclassify/prove global maintenance authority** |
| Subscription expiry | Celery Beat | global subscription lifecycle | worker | ordinary worker session performs global scan | no tenant context; tenant runtime hardening does not grant worker global subscription authority | **GAP — fail-closed/nonfunctional path to redesign** |
| Trial lifecycle monitor | Celery Beat | global trial lifecycle | worker | ordinary worker session performs global scan | no dedicated current maintenance capability identified in P3E-A | **GAP — prove ACL/RLS then redesign as bounded maintenance** |
| Daily member reminders | Celery Beat | global member/subscription scan + WhatsApp | worker | ordinary worker repositories, no tenant context | external sends occur inline; no durable delivery/idempotency boundary identified | **GAP — tenant/maintenance + delivery semantics** |
| Daily digest | Celery Beat | global gym/member/attendance/payment scan | worker | ordinary worker session, then per-gym worker sessions | no tenant context installed before global/per-gym reads | **GAP — global discovery and tenant execution must be separated** |
| Branch-hours partition readiness | Celery Beat | database catalog health check | worker | read-only PostgreSQL catalog checks | no DDL; no tenant business mutation | **LOW RISK — preserve and certify identity** |
| Branch-hours projection rebuild helper | leased branch-hours worker | one tenant/branch | caller-owned worker transaction | caller must establish tenant + lease context | no session/commit created by helper | **REFERENCE / preserve** |

`PARTIAL` and `GAP` are discovery classifications, not proof of exploitability.
PostgreSQL runtime tests decide whether each legacy job currently fails closed or has
unexpected privilege. P3E must not broaden worker privileges merely to make a job run.

## Proven scheduler drift at the frozen parent

The frozen P3D Celery Beat configuration references two task names that are not
registered by the corresponding modules:

1. `app.tasks.expire_subs.run`
   - actual registered expiry task: `expire_subscriptions`
2. `app.tasks.reminders.run`
   - actual registered daily reminder task: `send_daily_reminders`

P3E-A corrects only those references and adds a static contract so a Beat target
cannot silently drift away from the task registry again.

## Required follow-on proofs

### P3E-B — principal routing
For each task family prove that worker, maintenance, API, auth, and migration
credentials cannot substitute for one another. No database URL fallback may turn a
worker into an API or privileged process.

### P3E-C — live identity attestation
Reprove worker/maintenance startup attestation on PostgreSQL 16, including wrong
login, role-membership drift, `SET ROLE`, and prohibited privilege-graph paths.

### P3E-D — tenant and session isolation
For tenant tasks test tenant A -> tenant B -> no-context -> tenant A, including
commit, rollback, cancellation, exception, and task retry. No tenant/principal/worker
GUC may survive into another unit of work.

### P3E-E — queue payload trust
Task IDs and object keys are locators only. Sensitive execution must reconstruct
authority from durable current state before database or object-store mutation.

Priority targets: organization assets, geocoding, and any task carrying tenant or
branch identifiers directly in the broker message.

### P3E-F — claims, leases, fencing, retry and idempotency
Re-certify the two hardened outboxes and determine the exact guarantees needed for
geocoding, assets, notifications, subscription lifecycle, and trial lifecycle.
Explicitly test worker crash, lease loss, stale worker resume, duplicate delivery,
out-of-order delivery, cancellation, and retry after partial external success.

### P3E-G — global scheduler / maintenance boundary
Legacy global scans must not be made functional by granting broad cross-tenant rights
to `worker_runtime`. Where global discovery is required, use the already-established
maintenance pattern: isolated identity, bounded capability, explicit context, bounded
batch, and tenant-bound downstream execution when appropriate.

Priority targets:

- subscription expiry;
- trial lifecycle;
- daily digest discovery;
- reminder discovery;
- orphan asset cleanup.

### P3E-H — external side effects
WhatsApp, S3 and future webhook/email/provider delivery must be evaluated for
post-commit ordering, durable delivery state, retry classification, idempotency,
duplicate suppression, timeout behavior, and secret/PII-safe logging.

### P3E-I — provenance and observability
Every security-relevant async operation should make job/task ID, attempt, tenant,
entity, worker identity, claim/lease, correlation/trace and originating actor
recoverable without logging secrets or raw protected identifiers.

## Migration rule

P3E-A authorizes **no database privilege expansion**.

A P3E migration is permitted only if fresh PostgreSQL evidence proves an operation
needs a new bounded database capability. Any such migration must preserve reduced
roles, ENABLE + FORCE RLS, no `BYPASSRLS`, no ownership escalation, no PUBLIC DML,
clean lineage, reversible downgrade, and same-SHA migration certification.

## Static contract introduced by P3E-A

`tests/test_p3e_async_identity_static_contracts.py` provides a source-only gate that:

1. derives registered Celery task names from every `app/tasks/*.py` task decorator;
2. derives every Beat target from the canonical Celery configuration;
3. rejects any Beat target that is not actually registered;
4. verifies every maintenance task is registered; and
5. verifies every scheduled maintenance task explicitly targets
   `MAINTENANCE_QUEUE`.

This is intentionally source-only: it can run before broker/database startup and
catches configuration drift without weakening runtime security.

## P3E-A exit condition

P3E-A is complete only when:

- this inventory is reviewed against the exact P3D parent;
- the static task-registry contract is green;
- the two scheduler-name corrections are verified;
- P3D remains unchanged;
- no DB/RLS/ACL privilege is broadened; and
- subsequent runtime remediation is tracked as P3E work, not hidden inside the
  inventory step.
