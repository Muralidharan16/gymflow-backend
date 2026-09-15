# P8 runbook — Stuck lifecycle transition

## Alert and ownership

Alert: `DoersLifecycleStuck`  
Owner: `platform-runtime`  
Failure mode: `stuck_saga_or_lifecycle`

## Customer impact

A branch or related aggregate can remain in a transitional lifecycle state, blocking dependent operations and leaving durable follow-up work pending.

## Evidence to preserve

Preserve the authoritative lifecycle row/state, transition timestamps, parent command/outbox record, lease/fence fields, attempt history, correlation/saga/task IDs, maintenance-worker logs and any compensation evidence.

## Diagnosis

1. Identify the aggregate classified `stuck` by the lifecycle watchdog and read its current PostgreSQL state.
2. Inspect the parent lifecycle command/outbox record and current lease/fence owner.
3. Confirm whether a live worker still owns the transition or whether the lease has expired.
4. Inspect downstream durable work and compensation state before deciding whether the original transition can resume.
5. Correlate the transition with worker loss, Redis/broker outage, database disconnect, provider failure or deterministic application error.
6. Determine whether the certified lifecycle repair/replay path admits this exact state transition.

## Safe first actions

- Allow the normal maintenance sweep to recover an expired/stale lease when its certified rules cover the state.
- Use only the admitted lifecycle repair/replay path after proving no valid live owner exists.
- Preserve the original logical identity and transition preconditions.
- If compensation is required, invoke only the certified compensation path and retain its audit evidence.

## Forbidden actions

- Never update lifecycle state directly in SQL to skip a transition.
- Never clear a live lease/fence simply because progress appears slow.
- Never create a second competing lifecycle command for the same obligation.
- Never infer recovery state from Redis alone; PostgreSQL remains authoritative.

## Escalation

Escalate when the transition survives one maintenance sweep, exceeds 45 minutes, repeatedly re-enters `stuck`, or compensation/reconciliation is ambiguous.

## Recovery verification

Verify the authoritative lifecycle state is no longer transitional/stuck, the parent durable command is resolved consistently, no competing lease exists, dependent work resumes, compensation/replay produced no duplicate effect, and the alert condition remains clear through the next maintenance sweep.
