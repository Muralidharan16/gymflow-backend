# P8 runbook — Finance reconciliation mismatch

## Alert and ownership

Alert: `DoersFinanceReconciliationMismatch`  
Owner: `finance-reliability`  
Failure mode: `finance_reconciliation_mismatch`

## Customer impact

Authoritative Finance state disagrees with expected provider or accounting evidence. Blind correction can create duplicate payments, credits, refunds, invoice effects or inconsistent audit history.

## Evidence to preserve

Preserve the Finance obligation/payment/invoice/refund identifiers, immutable audit history, idempotency identity, provider references/events, reconciliation snapshots, current status, timestamps, correlation/saga/task IDs and all operator decisions. Do not expose sensitive financial credentials in incident notes.

## Diagnosis

1. Identify the exact mismatch class from the PostgreSQL Finance snapshot and authoritative Finance records.
2. Reconstruct expected state from persisted Finance/accounting rules rather than dashboard values.
3. Compare persisted provider references/evidence with provider state where reconciliation is supported.
4. Inspect idempotency reservation/completion history and any provider-ack ambiguity.
5. Determine whether the mismatch is stale observation, missing acknowledgement, duplicate external evidence, accounting projection lag or a genuine business-state contradiction.
6. Freeze automated mutation for the affected obligation if further processing could compound the discrepancy.

## Safe first actions

- Preserve all Finance/provider evidence before attempting repair.
- Use the certified reconciliation path to converge authoritative state.
- Reuse the same logical idempotency identity when a certified retry is permitted.
- If provider outcome is ambiguous, keep the local state non-terminal until reconciliation establishes the result.

## Forbidden actions

- Never manually mark a payment/refund/invoice effect successful without authoritative evidence.
- Never create a replacement refund/payment merely because the original state is unclear.
- Never edit monetary amounts, provider references or idempotency identities from operator-supplied values.
- Never activate the deferred refund-provider execution path under this runbook.

## Escalation

Escalate immediately when the mismatch involves issued invoices, payments, credits, refund obligations or money movement; otherwise escalate if it persists for 5 minutes or grows after one reconciliation attempt.

## Recovery verification

Verify PostgreSQL Finance state, accounting evidence and provider evidence agree; reconciliation mismatch depth returns to zero for the obligation; idempotency history shows no duplicate logical effect; no extra payment/refund/credit was created; and operator evidence is retained for audit.
