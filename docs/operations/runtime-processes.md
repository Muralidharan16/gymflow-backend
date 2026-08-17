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

## P4C durable notification boundary

P4C admits lifecycle member email as a real external effect through the shared branch outbox. Legacy reminder and daily-digest entry points remain fail-closed because their old global discovery implementations are not tenant-bound durable command producers. WhatsApp also remains outside the admitted P4C delivery channel until it receives equivalent provider evidence and callback semantics. `branch.refund_required` remains deferred to P4D.

The lifecycle `branch.member_notification` event does not authorize a recipient. Under a live worker lease, `app_secure.materialize_branch_member_notifications(...)` re-reads authoritative branch history, branch metadata, active members and current communication preferences from PostgreSQL, creates one deterministic command per eligible member, and supersedes the parent fanout event. The child `notification.delivery` command contains only an internal command identifier; delivery claim re-reads the current member email and suppression state immediately before provider I/O.

The ordinary worker is the only process allowed to receive `P4C_RESEND_API_KEY` and `NOTIFICATION_EMAIL_PROVIDER_MODE=resend`. The API must receive neither. Conversely, only the API receives `RESEND_WEBHOOK_SECRET`; the ordinary worker, maintenance worker, beat and Flower must not receive it. The public webhook path `/webhooks/notifications/resend` is exempt from tenant/JWT authentication only because it authenticates the untouched raw request body with the Resend/Svix signature before invoking any database capability. Tenant, member, destination and message fields from the webhook are never authorization authority.

Production notification workers must receive `NOTIFICATION_METRICS_OTLP_ENDPOINT` and bounded export interval/timeout values. The Resend adapter initializes the process-local OTLP/HTTP metric reader before its first network request. Missing or invalid telemetry configuration is a retryable operational failure and **no provider request is issued**. Notification metrics use low-cardinality provider/operation/outcome/channel/result attributes only; member IDs, tenant IDs, email addresses, provider message IDs and request bodies are forbidden metric dimensions.

Provider HTTP acceptance is explicitly non-terminal. A successful Resend POST stores the provider email ID plus request/evidence hashes and moves the command/outbox to `provider_accepted`; it does not mean `delivered`. Terminal success requires a verified provider event or reconciliation result. Duplicate provider events are idempotent. Stale events are retained as immutable provider evidence but do not rewind current truth. Once a message is proven delivered, a later complaint/bounce/suppression signal may suppress future communication while historical delivery remains `succeeded/delivered`.

A worker crash after delivery claim is handled by lease fencing. The old worker cannot acknowledge after its lease expires. Reclaiming an expired in-flight command records the abandoned attempt as an `ambiguous_outcome` (`worker_lease_expired_commit_unknown`) and retries with the same deterministic logical idempotency key. If provider acceptance is known, retries do not blindly submit another logical notification; the provider reference enters reconciliation.

### P4C reconciliation

Global discovery belongs only to the maintenance identity. Every five minutes the maintenance worker calls the bounded `app_secure.enqueue_notification_reconciliation(...)` capability, which uses `FOR UPDATE SKIP LOCKED` and enqueues provider checks for old `provider_accepted` commands. Maintenance never receives the Resend send credential and never queries the provider itself. The ordinary worker claims `notification.reconcile`, performs the provider GET, and persists the evidence through a fenced capability. Non-terminal provider state schedules another bounded check; terminal provider state updates the command through the same monotonic evidence rules as the signed webhook.

Maintenance also exports a PII-free operational snapshot through `app_secure.notification_operational_snapshot()`: pending/provider-accepted depth, dead-letter depth and oldest outstanding age. Alert on sustained provider failures, provider latency, provider-accepted age, reconciliation failure, backlog age and DLQ growth rather than relying on HTTP error rate alone.

### P4C dead-letter/operator recovery

`dead_lettered` does not authorize blind resend. Operators first inspect the command identifier, attempts/evidence, provider status and root cause. The maintenance-only `app_secure.list_notification_dead_letters(...)` returns bounded identifiers/status/reason metadata without message bodies or destinations.

After a proven no-effect failure is corrected, an operator may call `app_secure.requeue_dead_lettered_notification(command_id, reason)`. That capability re-reads current member eligibility/contact preferences, keeps the existing deterministic command/idempotency identity, records an immutable `notification_operator_actions` audit row and restores a bounded retry budget. It accepts no arbitrary destination or message body.

Replay is deliberately refused when a provider reference exists or when any attempt is ambiguous, provider-accepted or otherwise proves that an external effect may already exist. Such commands must be resolved through provider reconciliation; manually editing `notification_commands`, deleting attempt evidence, changing provider IDs or sending directly through Resend is forbidden because it can counterfeit convergence or duplicate member communication.

## Container deployment

`docker-compose.yml` is a development convenience configuration. Apply `deploy/docker-compose.production-identities.yml` (or reproduce the same environment isolation in Kubernetes/systemd/another orchestrator) for production identity compartmentalization.

Do not place all database URLs or external-provider credentials in one shared production secret bundle and rely on application code to choose the correct one. Secret distribution is part of the security boundary.

## Startup expectations

1. Infrastructure provisions PostgreSQL capability roles using the canonical cluster-role bootstrap.
2. Deployment logins are provisioned with the exact runtime binding contract.
3. Alembic runs separately as `migration_owner` and reaches the single repository HEAD.
4. API/worker/maintenance startup attestation verifies the live PostgreSQL principal before work begins.
5. The ordinary worker receives the P4B OpenSearch/search-telemetry configuration and, when P4C email is enabled, the P4C Resend send credential plus notification telemetry configuration.
6. The API receives only the P4C Resend webhook verification secret; maintenance receives only notification operational telemetry; beat and Flower receive neither notification credential.
7. RLS remains enabled and forced on governed tenant tables; no production process disables it.

A green application health check is not a substitute for these identity proofs.