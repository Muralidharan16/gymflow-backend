# PAY-18 Runbook — Refund Ambiguity

## Alert and ownership

Alert: `DoersPay18RefundAmbiguity`  
Owner: `finance-reliability`  
Failure mode: `refund_ambiguity`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

A refund may have succeeded at the provider while local acknowledgement is uncertain, or a Platform Billing refund remains unknown. Blind resubmission can credit the customer twice.

## Evidence to preserve

Preserve refund intent/obligation, execution command, lease fence, provider evidence, credit note linkage, reconciliation state, payment/refund totals, and sanitized Finance correlation.

## Diagnosis

1. Determine whether refund_unknown_total comes from Platform Billing status=unknown, Finance reconciliation_pending, or both.
2. Inspect the durable refund obligation and execution command using the certified reduced refund/reconciliation identities.
3. Determine whether the provider request was never sent, definitely failed before send, or may have succeeded.
4. Query provider refund evidence and verify amount, currency, payment reference binding, and provider environment.
5. Check credit note/refund financial entries to ensure local financial finalization has not already occurred.
6. Check worker crash/redelivery history and lease fence when ambiguity followed process death or broker acknowledgement loss.

## Safe first actions

- Keep ambiguous commands non-retryable until reconciliation determines the provider outcome.
- Use PAY-10/PAY-17 refund reconciliation and crash-recovery paths; retain the original logical obligation key.
- Complete financial finalization exactly once after authoritative provider success is proven.

## Forbidden actions

- Never blindly resubmit a refund with unknown provider outcome.
- Never cancel or mark a refund successful manually to bypass reconciliation.
- Never remove the refund obligation, command, credit note, or provider evidence to clear the backlog.

## Escalation

Escalate any ambiguous refund immediately to Finance reliability. Involve provider support when lookup cannot establish a final result, and security when amount/currency/account/environment evidence is inconsistent.

## Recovery verification

Verify one provider refund effect, one local financial finalization, preserved refund obligation, correct credit note/accounting entries, no duplicate customer credit, and refund_unknown_total returns to zero through reconciliation rather than record deletion.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
