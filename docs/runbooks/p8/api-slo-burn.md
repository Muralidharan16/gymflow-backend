# P8 runbook — API availability or latency SLO burn

## Alert and ownership

Alert: `DoersApiSloBurn`  
Owner: `api-runtime`  
Failure mode: `api_availability_or_latency_slo_breach`

## Customer impact

API 5xx errors or slow requests are consuming the 30-day reliability budget faster than allowed. Customer actions may fail or become unacceptably slow even when the service remains partially available.

## Evidence to preserve

Preserve fast/slow burn recording-rule values, route/status-class distributions, latency histograms, readiness/drain state, database pool health, Redis state, provider metrics, deployment/process events and representative request/correlation IDs from structured logs.

## Diagnosis

1. Separate availability burn from latency burn and determine whether the fast or slow burn path fired.
2. Identify the bounded route templates/status classes contributing most strongly; do not introduce entity IDs as metric labels.
3. Check readiness/drain state and confirm maintenance/deploy transitions are not being misclassified as business failures.
4. Correlate with database pool wait/exhaustion, Redis health, worker backlog and provider latency/errors.
5. Inspect recent code/config/runtime changes without assuming they are causal.
6. Decide whether the incident is capacity, dependency, application error, database contention or provider degradation before mitigation.

## Safe first actions

- Restore the failing dependency or unhealthy process through certified orchestration controls.
- Use graceful drain/load-shedding mechanisms when necessary to protect in-flight work.
- Roll back only through the approved deployment/release process; this runbook itself does not authorize a release or deployment.
- Prioritize the dominant route/error class and verify improvement against burn-rate signals.

## Forbidden actions

- Never disable readiness, security, tenant isolation or rate-limit protections merely to improve the SLO graph.
- Never convert 5xx failures into false success responses.
- Never create high-cardinality metric labels for incident drill-down; use logs/traces.
- Never perform an unapproved production deployment under P8 certification authority.

## Escalation

Escalate when fast burn survives 5 minutes, slow burn survives 30 minutes, remaining monthly API budget falls below 25%, or the incident affects authentication, Finance or broad tenant availability.

## Recovery verification

Verify both fast and slow burn conditions clear, request success/latency distributions recover, readiness/dependency metrics are healthy, error-budget consumption returns to a sustainable rate and no safety/security invariant was weakened during mitigation.
