# P8 runbook — Redis or broker unavailable

## Alert and ownership

Alert: `DoersRedisOrBrokerUnavailable`  
Owner: `platform-runtime`  
Failure mode: `redis_or_broker_unavailable`

## Customer impact

Session/rate-limit coordination and Celery delivery can degrade or stop. Durable PostgreSQL business obligations remain authoritative and must survive the coordination outage.

## Evidence to preserve

Preserve the failing Redis role, TLS/auth/topology evidence, reconnect logs, worker/broker state, queue depth/age, scheduler ownership, application readiness behavior and representative durable PostgreSQL obligations.

## Diagnosis

1. Identify whether `application`, `broker` or `result_backend` health failed.
2. Verify the configured Redis topology, TLS certificate/hostname, authentication and endpoint reachability.
3. Determine whether failure is local process connectivity, Redis failover/topology, network loss or credential/configuration drift.
4. Check worker reconnect behavior and whether queues are accumulating while PostgreSQL obligations remain intact.
5. Inspect Beat ownership if the broker/coordination plane is affected.
6. Confirm the outage did not trigger unsafe local business-state substitutions.

## Safe first actions

- Restore the certified Redis endpoint/topology at the infrastructure layer.
- Allow Celery's configured reconnect/retry behavior to recover delivery after the broker returns.
- Keep durable commands pending/retryable in PostgreSQL during the outage.
- After recovery, watch queue age/redeliveries and reconcile ambiguous external effects before replay.

## Forbidden actions

- Never mutate durable PostgreSQL state to compensate for missing Redis delivery evidence.
- Never weaken TLS/authentication or switch to an unapproved Redis endpoint.
- Never force-ack queued work or disable late acknowledgements/reconnect protections.
- Never run an unfenced duplicate Beat scheduler to compensate for broker loss.

## Escalation

Escalate if the broker remains unavailable for 5 minutes, workers cannot reconnect after service recovery, queue age breaches, or scheduler ownership remains unavailable/contended.

## Recovery verification

Verify all required Redis roles report healthy, workers reconnect, scheduler ownership is singular, queued durable work resumes without duplicate effects, queue age is falling and PostgreSQL state remains authoritative/consistent.
