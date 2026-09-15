# P8-A — Alerts and SLOs

P8-A turns the P8-M telemetry surface into machine-testable operational decisions without changing business authority. PostgreSQL remains the durable authority for business work; Redis/Celery/Beat remain coordination surfaces; alerting and telemetry remain evidence only.

## Parent boundary

P8-A is anchored to certified P8-M head `5ef26cd1698e12589b586596b60641bbeba507bd`, tree `4c6eb08ba4b93352c45ee302910f37c2d37d2006`.

The P8-A head must re-prove P8-G, P8-L and P8-M. Any instrumentation change needed to make a critical failure observable is therefore included in the same-head P8-M reproof rather than being accepted as an unverified alert-only patch.

## Rule sources

- `docs/architecture/p8_alert_slo_contract.json` is the machine-reviewable policy contract.
- `ops/observability/p8_rules.yml` is the Prometheus-compatible recording/alert rule bundle expected after OTLP metric-name translation.
- P8-R will populate the reserved `docs/runbooks/p8/*.md` targets. P8-A freezes those links now but does not claim `P8_RUNBOOK_COVERAGE=PASS`.

No public metrics or rule endpoint is introduced by P8-A.

## Critical failure coverage

Exactly the frozen P8-G critical failure modes are mapped to alert rules: durable dead letters, stuck lifecycle work, queue-age breach, Finance reconciliation mismatch, provider-success/DB-ack ambiguity, database pool exhaustion, database disconnect storms, Redis/broker outage, worker outage/redelivery storms, scheduler ownership risk, stale/failed backups, API SLO burn, provider SLO burn and observability-pipeline loss.

Each alert freezes an owner, critical severity, exact query, threshold, sustain window, deduplication key, customer-impact statement, runbook target, safe first action and escalation condition. Metric labels remain bounded; request, correlation, tenant, branch, principal, saga, task, trace and span identifiers are not admitted into alert metric selectors.

## Telemetry pipeline heartbeat

P8-A adds one bounded metric required to make telemetry loss itself observable: `doers.platform.observability.heartbeat{profile}`. The only profile values admitted by the recorder are the finite process-profile enum. The gauge is set after successful process-local P8 metric bootstrap and is exported by the periodic metric reader even when a process is otherwise idle.

Missing heartbeat from any required production `api`, `worker`, `maintenance` or `beat` profile for five minutes is a critical alert. The heartbeat is evidence only; missing telemetry never authorizes mutation of durable business state.

## SLOs

The 30-day SLO contract is:

| SLO | Objective | Good event/observation |
|---|---:|---|
| API availability | 99.9% | Business API request is not 5xx |
| API latency | 99.0% | Business API request completes in at most 1000 ms |
| Durable queue freshness | 99.9% | One-minute observation has oldest actionable work at most 300 s |
| Lifecycle completion | 99.9% | One-minute observation has zero transitions classified stuck after 900 s |
| Finance reconciliation | 99.99% | One-minute observation has zero authoritative reconciliation mismatches |
| Provider success | 99.5% | Classified provider call succeeds |
| Backup freshness | 99.9% | One-minute observation has verified DB backup age at most 86400 s and no failure evidence |

API availability, API latency and provider success use explicit multi-window burn-rate recording rules. A 14.4x error-budget burn over both 5 minutes and 1 hour is the fast-burn condition; a 6x burn over both 30 minutes and 6 hours is the slow-burn condition. These are critical operational alerts, not automatic business-state controls.

## Error-budget policy

When remaining monthly budget falls below 25%, the owning team must review nonessential changes and active reliability risks. An exhausted budget requires incident escalation and a nonessential change freeze. This policy never changes Finance state, queue acknowledgement, lifecycle state, provider acknowledgement or refund authority automatically.

## Backup truth boundary

Backup success/freshness remains infrastructure-owned. The application does not fabricate a successful backup. P8 expects the external backup system to ingest bounded `backup_class`, age and failure evidence into the same metrics backend. Absence of the database backup signal itself is alertable.

## P8-A certification markers

A passing same-head P8-A workflow emits:

- `P8_CRITICAL_ALERT_COVERAGE=PASS`
- `P8_SLO_CONTRACT=PASS`
- `P8_ALERT_ACTIONABILITY=PASS`
- `P8_OBSERVABILITY_PIPELINE_ALERT=PASS`
- `P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS`
- `P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P8-A does **not** emit `P8_RUNBOOK_COVERAGE=PASS`, `P8_PRODUCTION_LIKE_OBSERVABILITY=PASS` or the final P8-F marker. Those remain P8-R, P8-O and P8-F responsibilities respectively.

No P8-A success authorizes tag, release, deployment, refund-provider activation or live money movement.
