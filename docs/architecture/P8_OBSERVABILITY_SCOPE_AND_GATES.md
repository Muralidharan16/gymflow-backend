# P8 — Observability and operational response

## Baseline

Phase 8 starts only from merged and post-merge-certified P7 `main` commit `c0e18944b15bee2ae7b37b9ede86a7832a232c59`, tree `0edf7e8f997bfe8ca44af474f5f8b93b30a748ea`.

P8 does not change business authority. PostgreSQL remains the durable authority for business work. Redis, Celery and Beat remain coordination/delivery surfaces. Observability data is evidence, not business truth. Refund-provider execution remains deferred and fail-closed.

## Slices

- **P8-G — Governance and failure-mode inventory.** Freeze observability context, redaction policy, metrics domains, critical failure modes, SLO obligations, actionability requirements and hard stops.
- **P8-L — Structured logging and redaction.** JSON structured logging; request/correlation, tenant, branch, principal, saga, task, trace and span context where available; recursive pre-sink redaction; safe exception reporting.
- **P8-M — Metrics and instrumentation.** API, database, queue, lifecycle, Finance, provider and platform metrics with bounded-cardinality labels.
- **P8-A — Alerts and SLOs.** Machine-testable recording/alert rules, thresholds, burn-rate or sustained-window semantics, owners and escalation metadata.
- **P8-R — Operational runbooks.** One actionable runbook for every critical failure mode, with diagnosis, safe first actions, escalation and recovery verification.
- **P8-O — Production-like observability proof.** Inject representative failures in real processes/dependencies and prove signals, alerts, correlation and runbook binding.
- **P8-F — Final same-head certification.** Bind every P1-P7 inherited gate plus all P8 gates to one immutable SHA.

## Structured context contract

Structured logs and traces must carry the following fields when applicable: `request_id`, `correlation_id`, `tenant_id`, `branch_id`, `principal_id`, `principal_type`, `saga_id`, `task_id`, `trace_id`, and `span_id`. Missing/non-applicable values must be represented explicitly rather than inferred from unrelated headers.

Identity fields must come from already-authenticated request/task authority. In particular, an untrusted caller-supplied tenant/branch/principal header must never override the authenticated state solely for observability.

Request, correlation, principal, saga, task, trace and span identifiers are **forbidden as metrics labels** because they are unbounded/high-cardinality. Tenant and branch identifiers are also forbidden as default metric labels; tenant/branch drill-down belongs in logs/traces or deliberately bounded reporting paths.

## Redaction contract

Redaction must happen before data reaches any log, trace, error-reporting or telemetry sink. It must recursively cover structured dictionaries/lists and exception/breadcrumb payloads. At minimum the redaction policy covers:

- Authorization headers, cookies, access/refresh tokens and session material.
- Passwords, secrets, API keys and database/provider credentials.
- Webhook/provider signatures and sensitive payment credentials.
- Granular location PII.
- Email addresses, phone numbers and government identifiers where present in arbitrary payloads.

Redaction must preserve useful operational metadata while replacing sensitive values with `[REDACTED]`. Redaction failures must fail safe; observability must never justify emitting raw secrets.

## Metrics contract

P8 must expose bounded-cardinality signals for:

- API request volume, status/error rate, latency, in-flight requests, readiness and drain rejection.
- Database pool checked-out/utilization, wait/timeout and disconnect behavior.
- Queue depth, oldest-message age, redeliveries, dead letters and worker availability.
- Lifecycle pending/stuck/failed/replayed/compensation state.
- Finance reconciliation mismatch, idempotency conflict, provider-ack ambiguity and refund-obligation state without exposing sensitive financial details.
- Provider request/error/latency/timeout/rate-limit/circuit-open state.
- Redis health, scheduler ownership and backup freshness/failure state.

A metrics scrape path must not weaken the P1-P7 security model. Public unauthenticated Internet exposure is forbidden; deployment must provide an explicit network or authentication boundary.

## Critical failure-mode visibility

The final P8 gate must prove all of the following are visible and actionable: dead letters; stuck sagas/lifecycle transitions; queue-age breach; Finance reconciliation mismatch; provider-success/DB-ack ambiguity; database pool exhaustion; database disconnect storm; Redis/broker outage; worker outage or redelivery storm; duplicate scheduler-effect risk; backup failure/staleness; API SLO breach; provider SLO breach; and failure of the observability pipeline itself.

For each critical alert, actionability means an owner, severity, exact signal/query, threshold, sustain window, deduplication key, customer-impact statement, runbook link, safe first actions and explicit escalation condition.

## SLO contract

P8 must define machine-reviewable SLOs for API availability, API latency, durable queue freshness, lifecycle completion, Finance reconciliation, provider success rate and backup freshness. Thresholds must be operationally meaningful and paired with an error-budget/escalation policy; a dashboard-only number is not an SLO.

## Decisive evidence

Static tests may validate schemas, labels and redaction helpers, but they are not sufficient for P8-O. Decisive evidence must include real request-context propagation, real task-context propagation, metrics scraping, deliberate failure injection and proof that the expected critical signal/alert is produced. Mock-only observability proof is forbidden.

## Hard stops

P8 must stop rather than certify if any of the following occurs:

1. Raw secret or sensitive PII reaches logs, traces, errors or metrics.
2. High-cardinality identifiers become metrics labels.
3. An observability sink outage changes durable business authority or makes critical business work unrecoverable.
4. Metrics are exposed publicly without an explicit network/authentication boundary.
5. Any P1-P7 security, Finance, worker, Redis or graceful-deployment invariant is weakened.
6. Database credentials or runtime privileges are broadened for observability.
7. Refund-provider execution is enabled, live money moves, or a release/deployment is performed under P8 certification authority.

The final gate is simple: **every critical failure mode is visible and actionable on the exact same immutable head.**
