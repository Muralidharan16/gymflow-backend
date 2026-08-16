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

Production OpenSearch workers must also receive `SEARCH_METRICS_OTLP_ENDPOINT` plus bounded export interval/timeout values. P4B creates a process-local OTLP/HTTP metric reader before a production provider effect and emits low-cardinality request/outcome counts, end-to-end provider latency, and drift-repair decisions. Tenant IDs, branch IDs, provider document IDs and URLs are intentionally not metric attributes. Non-worker production profiles reject provider/telemetry configuration.

`SEARCH_PROVIDER_MODE=disabled` is intentionally fail-closed. If a search event reaches a worker without a configured provider, the event is recorded as a permanent provider rejection and is never marked delivered or synchronized. A production OpenSearch worker with invalid search telemetry configuration also fails closed before provider I/O; that operational failure remains retryable so corrected configuration can recover without inventing provider success.

The PostgreSQL branch projection/version is authoritative. Search events do not authorize their own operation: the worker re-reads the live leased projection and uses the branch UUID as the deterministic OpenSearch document ID. Index uses strict `version_type=external`; delete uses `version_type=external_gte` so an equal-version retry of an already-applied delete is idempotent while a stale delete still cannot remove a newer provider document. Every accepted/conflicting mutation is followed by a real-time provider GET before database acknowledgement. Equal-version index retries succeed only if the exact provider document is already present. Provider clock-ahead, document mismatch and unprovable delete outcomes are drift/failure states rather than local success.

Maintenance reconciliation has no direct provider-success authority. It may only enqueue bounded durable repair work through `app_secure.enqueue_branch_search_reconciliation(...)`; only the ordinary worker's provider-evidence acknowledgement can establish `search_last_synced_at`. Periodic reconciliation deliberately rechecks previously acknowledged branches so provider-side document loss is repaired instead of being hidden by local sync timestamps.

Provider/document drift carrying complete evidence is fenced by the worker-only `app_secure.repair_branch_search_provider_drift(...)` capability. The conflicted leased event is superseded, immutable failure/evidence history is preserved, the authoritative search version is advanced above the observed provider version, and exactly one fresh search command is queued. Unknown transport commit points never enter this repair path.

### P4B dead-letter/operator recovery

Search failures that remain retryable use the lifecycle outbox backoff policy; permanent failures or exhausted retries become `dead_lettered` and retain `last_error` plus the immutable `branch_search_effect_attempts` evidence history. Operators should investigate the provider/telemetry/configuration cause first. After correction, recovery must create/requeue durable search work through the normal maintenance/reconciliation boundary; operators must not manually set `search_last_synced_at`, `search_provider_ack_version`, or fabricate provider evidence. Provider drift should be repaired by the worker fencing path rather than direct table mutation.

For operational visibility, alert on search provider request failure outcomes, latency degradation, drift-repair activity, and lifecycle search events entering `dead_lettered`. A lack of current provider acknowledgement or reconciliation evidence is a degraded state even if the API remains healthy.

## Container deployment

`docker-compose.yml` is a development convenience configuration. Apply `deploy/docker-compose.production-identities.yml` (or reproduce the same environment isolation in Kubernetes/systemd/another orchestrator) for production identity compartmentalization.

Do not place all database URLs or external-provider credentials in one shared production secret bundle and rely on application code to choose the correct one. Secret distribution is part of the security boundary.

## Startup expectations

1. Infrastructure provisions PostgreSQL capability roles using the canonical cluster-role bootstrap.
2. Deployment logins are provisioned with the exact runtime binding contract.
3. Alembic runs separately as `migration_owner` and reaches the single repository HEAD.
4. API/worker/maintenance startup attestation verifies the live PostgreSQL principal before work begins.
5. The ordinary worker receives the P4B OpenSearch and OTLP search-metrics configuration; non-worker processes receive neither.
6. RLS remains enabled and forced on governed tenant tables; no production process disables it.

A green application health check is not a substitute for these identity proofs.
