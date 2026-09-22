# PAY-18 Runbook — Chargeback

## Alert and ownership

Alert: `DoersPay18ChargebackOpen`  
Owner: `finance-reliability`  
Failure mode: `chargeback`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

An external chargeback creates potential financial liability and freezes conflicting payment/refund actions until evidence and provider decision are handled.

## Evidence to preserve

Preserve the PAY-13 dispute, append-only evidence/events, financial liability entries, payment/invoice binding, provider decision deadlines, settlement/accounting evidence, and sanitized Finance correlation.

## Diagnosis

1. Confirm the dispute type is chargeback and identify its current PAY-13 lifecycle state through authorized tooling.
2. Verify the disputed payment was historically captured and the dispute amount/currency exactly bind that payment.
3. Review evidence_due_at and all append-only evidence already attached before taking action.
4. Confirm the financial hold is active for unresolved states and no conflicting refund execution is proceeding.
5. Check accounting reconciliation for chargeback or settlement mismatches tied to the same financial period.
6. Review provider status/decision evidence and ensure the event source is authenticated and deduplicated.

## Safe first actions

- Maintain the PAY-13 financial hold while unresolved.
- Append evidence through the certified dispute evidence path and submit only complete, authorized evidence.
- Record provider decision and liability reversal/loss entries through PAY-13’s required same-transaction closure controls.

## Forbidden actions

- Never release the financial hold before PAY-13 allows it.
- Never edit or delete dispute evidence/events/financial entries.
- Never start a conflicting refund to bypass the chargeback process.

## Escalation

Escalate every open chargeback to the Finance dispute owner before provider evidence deadlines. Escalate suspected fraud or forged dispute/provider evidence to finance-security immediately.

## Recovery verification

Verify provider decision evidence is durable, dispute transitions are valid, financial hold is released only when allowed, required liability reversal/loss entry exists, accounting reconciliation is clean, and closed disputes are terminal.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
