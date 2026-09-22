# PAY-18 Runbook — Duplicate Payment Allegation

## Alert and ownership

Alert: `DoersPay18DuplicatePaymentAllegation`  
Owner: `finance-reliability`  
Failure mode: `duplicate_payment_allegation`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

A customer or provider alleges more than one charge for one intended obligation. Incorrect handling can create a second refund, conceal a duplicate, or alter valid invoice/entitlement history.

## Evidence to preserve

Preserve the PAY-13 dispute, all payment attempts/events, provider operation/idempotency evidence, invoice bindings, settlement evidence, customer allegation metadata already stored in approved evidence channels, and sanitized Finance correlation.

## Diagnosis

1. Confirm an unresolved duplicate_charge_allegation dispute exists and retain its financial hold/evidence state.
2. Compare local payment attempts and provider payment objects using authorized tools, not metric labels or copied identifiers.
3. Verify idempotency keys, request hashes, provider references, and timestamps to distinguish replay from two independent authorized payments.
4. Check settlement evidence for both alleged charges and confirm whether both actually settled.
5. Check prior refunds/credit notes before proposing any remediation so the same money is not returned twice.
6. Review PAY-17 callback/provider concurrency evidence if the allegation coincides with retries, timeouts, or worker crashes.

## Safe first actions

- Keep the dispute financial hold intact while evidence is incomplete.
- Append evidence through PAY-13’s approved immutable dispute workflow and use reconciliation to establish actual provider effects.
- If a duplicate is proven, route remediation through the certified refund/credit-note authority with maker-checker controls where applicable.

## Forbidden actions

- Never issue an ad hoc second refund based only on the allegation.
- Never delete or merge payment attempts/events to make the records appear singular.
- Never alter provider references, idempotency keys, dispute history, or accounting evidence manually.

## Escalation

Every allegation requires Finance review. Escalate to security/fraud teams if the two effects have different authorization context or suspicious identity evidence, and to the provider if duplicate provider objects or settlement entries are confirmed.

## Recovery verification

Close only after local, provider, settlement, and refund evidence reconcile. Verify the dispute has the required closure evidence, financial liability entries are correct, no duplicate refund exists, and the customer-facing resolution matches durable financial truth.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
