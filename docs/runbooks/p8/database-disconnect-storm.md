# P8 runbook — Database disconnect storm

## Alert and ownership

Alert: `DoersDatabaseDisconnectStorm`  
Owner: `platform-runtime`  
Failure mode: `database_disconnect_storm`

## Customer impact

Repeated database connection loss can interrupt API requests and worker progress, causing retries, latency and queue growth even if individual reconnections succeed.

## Evidence to preserve

Preserve disconnect counters by pool, database/server events, TLS/network evidence, failover timeline, pool recycle/reconnect logs, statement/transaction errors, readiness state, queue age and representative correlation IDs.

## Diagnosis

1. Identify which pools are disconnecting and the five-minute disconnect rate.
2. Check database service/failover status, DNS/network reachability and TLS validity before restarting application processes.
3. Determine whether disconnects correlate with server restart/failover, network loss, idle connection expiry, resource pressure or malformed connection lifecycle.
4. Inspect whether reconnects succeed and whether transactions are being retried only through certified semantics.
5. Correlate with API readiness, worker availability and queue age.
6. Confirm no database credential or runtime-principal drift occurred.

## Safe first actions

- Restore database/network/TLS health at the owning infrastructure layer.
- Allow healthy pool recycling/reconnect behavior to recover after the dependency stabilizes.
- Use existing graceful drain/restart controls for a demonstrably poisoned process pool; preserve in-flight durable work semantics.
- Escalate suspected database failover defects to the data reliability owner.

## Forbidden actions

- Never weaken TLS, database authentication, runtime principal or RLS requirements to reconnect.
- Never retry an unknown committed transaction by manually duplicating its business operation.
- Never mass-restart all runtime profiles before confirming the database dependency state.
- Never alter durable state merely to suppress retry errors.

## Escalation

Escalate if disconnect count doubles in the next five minutes, any production profile cannot reconnect, database failover does not converge, or ambiguous transaction outcomes appear.

## Recovery verification

Verify disconnect growth stops, all required profiles reconnect with the certified identity/TLS configuration, readiness is restored, transaction/business-state checks show no duplicate effects, and queue/API error indicators trend back to normal.
