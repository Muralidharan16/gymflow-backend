# P8 runbook — Dead letter present

## Alert and ownership

Alert: `DoersDeadLetterPresent`  
Owner: `platform-runtime`  
Failure mode: `dead_letter_present`

## Customer impact

Durable work has exhausted automated recovery. A customer-visible lifecycle, notification, search, outbox or other asynchronous obligation may remain incomplete until an operator determines whether a certified replay or reconciliation path is safe.

## Evidence to preserve

Preserve the authoritative PostgreSQL dead-letter row, original logical command/effect identity, attempt and lease history, last classified error, timestamps, correlation/saga/task identifiers from logs, and any provider evidence. Do not delete, rewrite or mark the row delivered merely to clear the alert.

## Diagnosis

1. Identify the bounded queue/domain from `doers.queue.dead_letters` and the corresponding PostgreSQL durable record.
2. Confirm the owning workflow and current authoritative state from PostgreSQL, not from Redis or a dashboard.
3. Inspect attempt count, last error classification, lease/fence ownership and whether another worker still owns recovery.
4. Correlate structured logs/traces using the stored command/correlation identity.
5. If an external provider was involved, inspect persisted provider references and determine whether the last outcome is known, failed or ambiguous.
6. Classify the incident as permanent failure, transient dependency failure, stale lease, provider ambiguity, or application defect before replay.

## Safe first actions

- Stop repeated unsafe manual retries for the affected logical obligation.
- If the certified replay path permits replay, wait for/verify lease expiry or release, then replay the same logical obligation with the original idempotency identity.
- For provider ambiguity, reconcile provider state before any new external mutation.
- Record the operator decision and evidence used.

## Forbidden actions

- Never delete the dead-letter record to make the metric green.
- Never mark work `sent`, `delivered`, `succeeded`, `refunded`, `indexed` or equivalent without authoritative evidence.
- Never change tenant, branch, destination, amount or provider identity from operator-supplied values.
- Never disable idempotency, lease or fencing protections to force replay.

## Escalation

Escalate when the dead letter remains unresolved for 10 minutes, repeats after one certified replay, indicates a deterministic code defect, or touches Finance/refund state. Finance/provider ambiguity escalates immediately to `finance-reliability`.

## Recovery verification

Verify the durable PostgreSQL obligation reaches the certified non-ambiguous state, the dead-letter depth returns to zero for that obligation without deleting evidence, queue age is stable or falling, no duplicate business/provider effect was created, and the incident/operator action is recorded.
