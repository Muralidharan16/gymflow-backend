# PAY-18 Runbook — Unknown Payment

## Alert and ownership

Alert: `DoersPay18UnknownPayment`  
Owner: `finance-reliability`  
Failure mode: `unknown_payment`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

The system cannot yet prove whether a payment provider operation succeeded. A duplicate retry can double-charge while premature failure can lose a successful customer payment.

## Evidence to preserve

Preserve the provider operation, request hash, attempt/lease fence, local payment event history, provider reconciliation evidence, invoice/subscription binding, and sanitized Finance correlation. Preserve timestamps and never edit the ambiguous row.

## Diagnosis

1. Confirm payment_unknown_total is non-zero and identify the incident through authorized Finance tools rather than metric labels.
2. Determine whether ambiguity arose before provider send, after provider success but before DB acknowledgement, timeout, malformed response, or reconciliation failure.
3. Query authoritative provider evidence using the certified reconciliation path and compare provider object, amount, currency, and environment.
4. Verify the local idempotency key/request hash and ensure no second provider operation has been submitted.
5. Check webhook inbox for later provider evidence that can resolve the unknown state safely.
6. Check invoice and entitlement projections only after payment truth is resolved; they are not authority for provider outcome.

## Safe first actions

- Use the PAY-17 reconciliation capability to move unknown state only when authoritative provider evidence proves the outcome.
- Keep customer communication neutral until money movement is proven; use sanitized correlation for cross-team tracing.
- If provider proves no object exists, close through the certified failed-final reconciliation path rather than ad hoc SQL.

## Forbidden actions

- Never blindly resubmit an unknown payment operation.
- Never change payment, invoice, or entitlement terminal state directly in the database.
- Never infer provider success from customer access, browser response, Redis state, or a metric alone.

## Escalation

Escalate immediately for any unknown payment, and involve the provider if authoritative lookup is unavailable or contradictory. Security escalation is required when environment/account/reference evidence does not match the configured provider boundary.

## Recovery verification

Require authoritative provider evidence, exactly one local/provider financial effect, resolved unknown state, consistent amount/currency/environment, and correct downstream payment application. Confirm the unknown metric decreases for the right reason rather than through evidence deletion.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
