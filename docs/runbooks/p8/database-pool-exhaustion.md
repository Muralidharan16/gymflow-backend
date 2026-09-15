# P8 runbook — Database pool exhaustion

## Alert and ownership

Alert: `DoersDatabasePoolExhaustion`  
Owner: `platform-runtime`  
Failure mode: `database_pool_exhaustion`

## Customer impact

API or worker processes may fail or wait too long to acquire database connections, increasing latency, 5xx errors and queue backlog.

## Evidence to preserve

Preserve pool utilization, checked-out count, acquisition-wait/timeout evidence, request/worker concurrency, slow-query/transaction evidence, database health/failover events and correlated readiness/API latency signals.

## Diagnosis

1. Identify the saturated runtime pool (`api`, `worker`, `maintenance` or `finance`).
2. Compare checked-out connections with configured capacity and current process concurrency.
3. Inspect long-running/idle-in-transaction work, blocked statements and dependency stalls that can hold connections.
4. Check whether database reachability, failover or network instability is causing connections to churn rather than return normally.
5. Correlate with API latency/readiness, queue age and worker health.
6. Determine whether the pressure is a leak, concurrency spike, slow database workload or downstream wait while holding a connection.

## Safe first actions

- Reduce the underlying connection-holding cause before changing pool capacity.
- Restore unhealthy database/network dependencies and allow normal pool recycling.
- Use existing load-shedding/drain mechanisms when necessary rather than bypassing safety controls.
- Scale process capacity only when database connection budget and deployment policy explicitly allow it.

## Forbidden actions

- Never increase pool size blindly beyond the database connection budget.
- Never kill transactions without identifying ownership and business impact.
- Never disable transaction, tenant, RLS or runtime identity protections to free connections.
- Never mark requests/work successful because database acquisition failed.

## Escalation

Escalate if timeouts continue for 5 minutes, utilization reaches 98%, readiness becomes dependency-unavailable, or evidence suggests a connection leak/deadlock affecting multiple profiles.

## Recovery verification

Verify utilization remains below the alert threshold, acquisition timeouts stop, wait latency normalizes, readiness/API latency recover, queue backlog is draining, and no transaction/business state was lost or manually forced during recovery.
