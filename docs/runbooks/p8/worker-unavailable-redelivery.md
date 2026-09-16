# P8 runbook — Worker unavailable or redelivery storm

## Alert and ownership

Alert: `DoersWorkerUnavailableOrRedeliveryStorm`  
Owner: `platform-runtime`  
Failure mode: `worker_unavailable_or_redelivery_storm`

## Customer impact

Asynchronous business work may stop progressing or repeatedly replay after worker loss. Certified late-ack/idempotency/fencing semantics are the protection against lost or duplicate effects and must remain intact.

## Evidence to preserve

Preserve worker/maintenance availability, task name/queue, redelivery counts, worker shutdown/lost evidence, broker reconnect history, task attempt/lease data, durable PostgreSQL command state and correlation/saga/task identifiers.

## Diagnosis

1. Identify whether the `worker` or `maintenance` profile is missing and which queues are affected.
2. Check broker connectivity and process/container/host health before changing task state.
3. Determine whether redeliveries follow expected SIGTERM/crash recovery or indicate repeated deterministic failure.
4. Inspect authoritative durable work and lease/fence state for representative tasks.
5. Correlate with database/Redis/provider outages and queue age.
6. Check for poison work/dead letters if the same logical task repeatedly redelivers.

## Safe first actions

- Restore a healthy replacement worker using the certified runtime configuration.
- Preserve `task_acks_late`, worker-lost rejection and prefetch semantics.
- Allow idempotent redelivery to recover work; use dead-letter/reconciliation paths for repeatedly failing obligations.
- Isolate deterministic poison work rather than repeatedly restarting all workers.

## Forbidden actions

- Never force-ack uncompleted durable work.
- Never disable late acknowledgements, worker-lost rejection, idempotency or lease fencing to reduce redelivery counts.
- Never manually duplicate a task whose previous external-effect outcome is ambiguous.
- Never purge the queue as a substitute for durable reconciliation.

## Escalation

Escalate if no healthy replacement worker appears within 5 minutes, redeliveries keep rising after dependency recovery, the same logical task repeatedly crashes workers, or any Finance/provider ambiguity is involved.

## Recovery verification

Verify required worker profiles are available, redelivery growth returns to baseline, queue age/depth are falling, durable PostgreSQL obligations progress once, no duplicate provider/business effect appears and any poison work has a preserved auditable recovery disposition.
