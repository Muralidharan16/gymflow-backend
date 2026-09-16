# P8 runbook — Provider success / database acknowledgement ambiguity

## Alert and ownership

Alert: `DoersProviderAckAmbiguity`  
Owner: `finance-reliability`  
Failure mode: `provider_success_db_ack_ambiguity`

## Customer impact

An external provider may have accepted or completed an effect while the local database acknowledgement is uncertain. A blind retry can duplicate an external effect, including a monetary one.

## Evidence to preserve

Preserve the durable command/effect row, logical idempotency identity, provider request/reference IDs, provider response/event evidence, timestamps, attempt/lease/fence history, Finance state, structured correlation/saga/task IDs and reconciliation results.

## Diagnosis

1. Identify the exact durable effect and confirm its current PostgreSQL state.
2. Confirm the last provider attempt and whether the failure occurred before submission, during submission, after provider acceptance or during local acknowledgement.
3. Query/reconcile provider state using the persisted provider reference/idempotency identity where supported.
4. Inspect current lease/fence ownership before considering any replay.
5. Determine whether authoritative evidence proves success, proves failure, or remains ambiguous.
6. Keep ambiguous effects non-terminal until reconciliation resolves them.

## Safe first actions

- Stop automatic/manual resubmission for the affected logical effect until reconciliation completes.
- Reconcile provider state using persisted authoritative references only.
- If certified recovery determines the provider did not execute the effect, replay the same logical obligation with the original idempotency identity and a valid fresh fence.
- Record the evidence and operator decision.

## Forbidden actions

- Never blindly repeat the provider mutation after an ambiguous outcome.
- Never create a replacement payment/refund/notification/search effect to bypass ambiguity.
- Never mark local state successful solely because a request was sent.
- Never alter provider reference, amount, destination, tenant or branch from operator input.
- Never activate deferred refund-provider execution under P8 authority.

## Escalation

Escalate immediately for monetary effects. Otherwise escalate when ambiguity remains after one reconciliation attempt, provider evidence is unavailable, or the same logical effect enters ambiguity more than once.

## Recovery verification

Verify provider evidence and PostgreSQL state converge on one non-ambiguous outcome, the same logical idempotency identity was preserved, no duplicate external effect exists, the ambiguity metric clears, and the full evidence trail remains auditable.
