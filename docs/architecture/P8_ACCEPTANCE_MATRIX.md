# P8 acceptance matrix

P8 certifies only when every marker below is emitted by same-head evidence and all inherited P1-P7 gates remain green.

| Area | Acceptance | Terminal marker |
|---|---|---|
| Governance | Exact merged P7 base/tree and frozen P8 scope | `P8_GOVERNANCE_OBSERVABILITY=PASS` |
| Structured logs | JSON/event structure carries authoritative request/tenant/branch/principal/correlation/saga/task context where applicable | `P8_STRUCTURED_LOG_CONTEXT=PASS` |
| Redaction | Secrets, tokens, cookies, signatures, credentials and sensitive PII are recursively redacted before every supported sink | `P8_SENSITIVE_REDACTION=PASS` |
| Metric cardinality | No request/correlation/principal/saga/task/trace/span IDs as metric labels; tenant/branch labels are not default | `P8_METRIC_CARDINALITY_BOUNDED=PASS` |
| API metrics | Request/error/latency/inflight/readiness/drain signals are scrapeable | `P8_API_METRICS=PASS` |
| DB metrics | Pool utilization/wait/timeout/disconnect signals are visible | `P8_DATABASE_METRICS=PASS` |
| Queue metrics | Depth/age/redelivery/dead-letter/worker signals are visible | `P8_QUEUE_METRICS=PASS` |
| Lifecycle metrics | Pending/stuck/failed/replay/compensation signals are visible | `P8_LIFECYCLE_METRICS=PASS` |
| Finance metrics | Reconciliation mismatch/idempotency/provider-ack/refund-obligation signals are visible without sensitive values | `P8_FINANCE_METRICS=PASS` |
| Provider metrics | Request/error/latency/timeout/rate-limit/circuit signals are visible | `P8_PROVIDER_METRICS=PASS` |
| Platform metrics | Redis/scheduler/backup signals are visible | `P8_PLATFORM_METRICS=PASS` |
| Alerts | Every frozen critical failure mode maps to a testable alert with owner/severity/query/threshold/window/dedupe/runbook | `P8_CRITICAL_ALERT_COVERAGE=PASS` |
| SLOs | API availability/latency, queue freshness, lifecycle completion, Finance reconciliation, provider success and backup freshness have thresholds and error-budget policy | `P8_SLO_CONTRACT=PASS` |
| Runbooks | Every critical alert has diagnosis, safe first actions, escalation and recovery verification | `P8_RUNBOOK_COVERAGE=PASS` |
| Production-like proof | Real request/task context, scrape and representative failure injection produce expected signals | `P8_PRODUCTION_LIKE_OBSERVABILITY=PASS` |
| Authority | Observability sink loss cannot become business authority or make durable work unrecoverable | `P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS` |
| Refund boundary | Provider execution remains disabled/fail-closed | `P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED` |
| Final | All inherited P1-P7 and P8 gates pass on one immutable SHA | `P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS` |

## Final hard gate

`P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS`

No passing P8 result authorizes tag, release, deployment, refund-provider activation or live money movement.
