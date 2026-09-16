# P8 runbook — Observability pipeline failure

## Alert and ownership

Alert: `DoersObservabilityPipelineFailure`  
Owner: `platform-runtime`  
Failure mode: `observability_pipeline_failure`

## Customer impact

One or more production runtime profiles are no longer producing the expected telemetry heartbeat. Business processing may still be healthy, but operators are partially blind and other alerts/SLOs can no longer be trusted until telemetry is restored.

## Evidence to preserve

Preserve the last heartbeat timestamp by profile, OTLP exporter/collector logs, runtime startup configuration, service health/readiness, network/TLS evidence to the telemetry endpoint, collector/backend health and any concurrent business/dependency alerts.

## Diagnosis

1. Identify which required profile heartbeat (`api`, `worker`, `maintenance`, `beat`) is absent.
2. Confirm the runtime process is actually alive and healthy independently of telemetry.
3. Check OTLP endpoint reachability, collector/backend health, TLS/network path and exporter errors.
4. Determine whether failure is process-local instrumentation, exporter/collector transport, backend ingestion or alert-rule evaluation.
5. Cross-check logs and business-authority sources directly while telemetry is degraded.
6. Treat unrelated business incidents as separate incidents; absence of telemetry must never be interpreted as healthy business state.

## Safe first actions

- Restore the telemetry collector/export path without changing business state.
- If only one runtime process has a poisoned exporter, use the approved graceful restart path after confirming durable work/replay safety.
- Use direct authoritative PostgreSQL/provider/infrastructure evidence for critical decisions while observability is impaired.
- Record the blindness window so post-incident analysis does not assume missing data means zero events.

## Forbidden actions

- Never stop or mutate durable business work merely to make telemetry return.
- Never weaken authentication/TLS or expose a public unauthenticated metrics endpoint as a workaround.
- Never infer success from missing metrics/logs/traces.
- Never fabricate heartbeat/backup/provider success telemetry.

## Escalation

Escalate when any required profile remains blind for 5 minutes, the collector/backend is broadly unavailable, multiple telemetry classes fail together, or another critical incident is active while observability is degraded.

## Recovery verification

Verify heartbeats resume for all required profiles, exporter/collector errors stop, alert/SLO evaluation is current again, no business authority was changed during the outage, and the blindness interval plus any direct-evidence decisions are recorded for audit/post-incident review.
