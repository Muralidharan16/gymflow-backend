# DOERS production runtime processes

This document is the deployment contract for production runtime processes. Development may intentionally use shared local credentials, but production must use the process profiles enforced by `app/core/config.py` and `security/runtime_identity/process_profiles.v1.json`.

## Process matrix

| Process | `DOERS_PROCESS_PROFILE` | Database credentials present | Celery queue/profile |
|---|---|---|---|
| FastAPI API | `api` | API + auth only | none |
| Ordinary Celery worker | `worker` | worker only | queue `worker`, profile `worker` |
| Lifecycle maintenance worker | `maintenance` | maintenance only | queue `lifecycle-maintenance`, profile `maintenance` |
| Celery beat | `beat` | none | scheduler only |
| Flower/control plane | `beat` | none | observation/control only |
| Alembic migration job | not an application process | migration owner only | none |

Production startup is fail-closed. A process that receives a database environment variable outside its profile must fail validation before serving traffic or consuming broker messages.

## Database identity rules

The deployment LOGIN roles are distinct from capability roles. Application capability roles remain NOLOGIN. Deployment logins inherit only their approved capability set and must not receive `SET ROLE`, ADMIN, superuser, create-role, create-database, replication, or BYPASSRLS capabilities.

API and auth are separate database identities even though they live in the same FastAPI process. Ordinary queue work and lifecycle maintenance are separate operating-system/process boundaries and must not receive each other's database secret. Beat and Flower do not need a database credential.

## P4B search-provider boundary

Branch search index/de-index is an ordinary worker responsibility. Production must configure the ordinary worker with `SEARCH_PROVIDER_MODE=opensearch`, `OPENSEARCH_URL`, `OPENSEARCH_INDEX`, the bounded request timeout, TLS verification, and provider credentials when the cluster requires basic authentication. The API/auth process, lifecycle-maintenance worker, beat and Flower do not need the OpenSearch credential and the production compose identity overlay explicitly blanks it for those processes.

`SEARCH_PROVIDER_MODE=disabled` is intentionally fail-closed. If a search event reaches a worker without a configured provider, the event is recorded as a permanent provider rejection and is never marked delivered or synchronized.

The PostgreSQL branch projection/version is authoritative. Search events do not authorize their own operation: the worker re-reads the live leased projection, writes the branch UUID as the deterministic OpenSearch document ID using strict external versioning, and performs a real-time provider GET before database acknowledgement. Equal-version retries succeed only if the exact provider document is already present. Provider clock-ahead, document mismatch and unprovable delete outcomes are drift/failure states rather than local success.

Maintenance reconciliation also has no direct provider-success authority. It may only enqueue bounded durable repair work through `app_secure.enqueue_branch_search_reconciliation(...)`; only the ordinary worker's provider-evidence acknowledgement can establish `search_last_synced_at`.

## Container deployment

`docker-compose.yml` is a development convenience configuration. Apply `deploy/docker-compose.production-identities.yml` (or reproduce the same environment isolation in Kubernetes/systemd/another orchestrator) for production identity compartmentalization.

Do not place all database URLs or external-provider credentials in one shared production secret bundle and rely on application code to choose the correct one. Secret distribution is part of the security boundary.

## Startup expectations

1. Infrastructure provisions PostgreSQL capability roles using the canonical cluster-role bootstrap.
2. Deployment logins are provisioned with the exact runtime binding contract.
3. Alembic runs separately as `migration_owner` and reaches the single repository HEAD.
4. API/worker/maintenance startup attestation verifies the live PostgreSQL principal before work begins.
5. The ordinary worker receives the P4B OpenSearch configuration; non-worker processes do not receive its credential.
6. RLS remains enabled and forced on governed tenant tables; no production process disables it.

A green application health check is not a substitute for these identity proofs.
