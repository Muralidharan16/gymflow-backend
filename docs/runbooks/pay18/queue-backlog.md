# PAY-18 Runbook — Financial Queue Backlog

## Alert and ownership

Alert: `DoersPay18QueueBacklog`  
Owner: `finance-reliability`  
Failure mode: `queue_backlog`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Verified webhooks, payment applications, Finance outbox events, or refunds are accumulating and can delay financial state propagation or customer resolution.

## Evidence to preserve

Preserve aggregate backlog snapshots, worker/broker/DB health, durable statuses and lease fences for sampled authorized records, dead-letter/retry evidence, and sanitized Finance correlation.

## Diagnosis

1. Identify which bounded backlog metric is growing: webhook, payment application, Finance outbox, or refund.
2. Check worker and maintenance process availability and Celery/Redis broker health.
3. Check PostgreSQL connectivity, pool pressure, lock waits, and deadlocks for the responsible runtime.
4. Inspect oldest durable work and lease/retry/dead-letter state through approved tooling without bulk mutation.
5. Check upstream provider outage or webhook delivery problems that may be creating work faster than it drains.
6. Check whether a poison command or repeated non-retryable failure is blocking progress and requiring reconciliation/manual review.

## Safe first actions

- Restore failed dependencies or worker capacity while preserving late-ack, lease, and idempotency behavior.
- Use certified reclaim/retry/reconciliation paths for eligible durable work.
- Throttle nonessential producers only through approved operational controls if backlog growth threatens financial recovery.

## Forbidden actions

- Never bulk-update queue statuses, leases, or attempt counters to drain metrics.
- Never delete pending/dead-letter financial work or evidence.
- Never disable idempotency, fencing, late acknowledgement, or maker-checker to increase throughput.

## Escalation

Escalate if backlog grows for ten minutes, any dead letter or unknown financial state appears, worker replacement cannot recover throughput, or database/provider incidents are contributing.

## Recovery verification

Verify backlog and oldest-age trends return to normal, workers are healthy, durable work has progressed through valid transitions, no work was lost, no duplicate financial effect occurred, and the metric decline matches authoritative database counts.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
