# P8 runbook — Scheduler ownership risk

## Alert and ownership

Alert: `DoersDuplicateSchedulerEffectRisk`  
Owner: `platform-runtime`  
Failure mode: `duplicate_scheduler_effect_risk`

## Customer impact

Beat ownership uncertainty can cause duplicate schedule dispatch attempts. Durable idempotency protects business authority, but repeated scheduling can increase load and external-effect risk.

## Evidence to preserve

Preserve all Beat process identities, Redis lease key/TTL/owner evidence, scheduler ownership metrics, dispatch timestamps, duplicate task observations, worker logs and durable idempotency/effect records for any suspected duplicate dispatch.

## Diagnosis

1. Enumerate all expected Beat processes and identify the current Redis lease owner.
2. Check for `contended` or `unavailable` ownership states and compare them with the configured lease TTL.
3. Determine whether a stale process, network partition, Redis failover or deployment overlap created contention.
4. Inspect scheduled task dispatch evidence for duplicates during the ownership window.
5. For any duplicate dispatch, verify durable idempotency/fencing prevented duplicate business effects.
6. Confirm the active scheduler uses the certified production configuration.

## Safe first actions

- Keep competing scheduler instances fail-closed and restore one valid lease owner.
- Terminate only the demonstrably stale/unauthorized duplicate process through normal orchestration controls.
- Preserve idempotency/fencing and let durable work deduplicate naturally.
- Reconcile any external effects that were attempted during ambiguous ownership.

## Forbidden actions

- Never disable or bypass scheduler ownership fencing to restore schedules.
- Never run multiple active Beat schedulers intentionally against the same schedule set.
- Never delete idempotency evidence to permit a duplicate dispatch.
- Never mark suspected duplicate external effects successful without reconciliation.

## Escalation

Escalate if contention lasts longer than one lease TTL, Redis ownership cannot become singular, duplicate dispatch is observed, or any duplicate attempt reaches a Finance/provider boundary.

## Recovery verification

Verify exactly one Beat owner remains, ownership metrics stay `owned` without contention through at least one lease renewal, expected schedules resume, duplicate dispatch stops and durable/provider evidence confirms no duplicate business effect was created.
