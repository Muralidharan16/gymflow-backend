# P8 runbook — Queue oldest-age breach

## Alert and ownership

Alert: `DoersQueueOldestAgeBreach`  
Owner: `platform-runtime`  
Failure mode: `queue_oldest_age_breach`

## Customer impact

Asynchronous work is falling behind the five-minute freshness objective. Notifications, lifecycle maintenance, search synchronization or other queued work may complete late even while durable PostgreSQL obligations remain intact.

## Evidence to preserve

Preserve queue name, depth and oldest-age time series, worker availability, Redis/broker health, representative durable PostgreSQL obligations, publish timestamps, redelivery evidence and correlated worker logs.

## Diagnosis

1. Identify which bounded queue has the highest `doers.queue.oldest_message_age` and whether depth is growing.
2. Check worker/maintenance availability and Redis broker health.
3. Sample authoritative PostgreSQL obligations to determine whether the backlog is delivery-only or durable work is also stuck.
4. Inspect redelivery rate, dead letters, long-running tasks and dependency latency.
5. Check whether the oldest observed broker message still corresponds to actionable durable work rather than already-resolved coordination residue.
6. Correlate the breach with deploy/drain events, worker restarts, database pressure or provider degradation.

## Safe first actions

- Restore missing certified worker/maintenance capacity when the runtime itself is unhealthy.
- Resolve the underlying dependency bottleneck before adding capacity blindly.
- Let idempotent durable work drain through normal routing; use replay only through certified recovery paths.
- If provider rate limiting is causal, honor provider backoff rather than increasing retry pressure.

## Forbidden actions

- Never purge a queue to clear the alert without proving each durable obligation is safely recoverable.
- Never force-ack work or disable late-ack/redelivery semantics.
- Never mutate PostgreSQL durable state solely to match Redis queue contents.
- Never bypass provider rate limits or idempotency controls.

## Escalation

Escalate when oldest age exceeds 900 seconds, continues increasing for 10 minutes after capacity/dependency recovery, produces dead letters, or affects Finance/provider reconciliation.

## Recovery verification

Verify oldest age falls below 300 seconds and continues decreasing/stable, worker profiles are healthy, durable PostgreSQL obligations are progressing, redelivery/dead-letter rates are not rising, and no work was lost or duplicated during recovery.
