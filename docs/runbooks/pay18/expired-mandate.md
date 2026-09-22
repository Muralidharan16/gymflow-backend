# PAY-18 Runbook — Expired Mandate

## Alert and ownership

Alert: `DoersPay18ExpiredMandate`  
Owner: `finance-reliability`  
Failure mode: `expired_mandate`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Recurring billing cannot lawfully or reliably charge through an expired mandate. Subscription renewal may enter dunning until a valid replacement authorization exists.

## Evidence to preserve

Preserve mandate lifecycle/replacement linkage, recurring billing job, dunning case/attempt evidence, provider mandate status evidence, customer notification evidence, and sanitized Finance correlation.

## Diagnosis

1. Confirm mandate_failure_total{state="expired"} reflects an unreplaced expired mandate rather than a superseded historical mandate.
2. Inspect the mandate lifecycle and verify expired_at/provider evidence through approved billing tooling.
3. Check replacement_mandate_id and confirm whether a replacement authorization is pending, active, failed, or absent.
4. Inspect the due recurring billing job and ensure it has not attempted an expired mandate.
5. Inspect the dunning case/stage and durable failure evidence if collection is already overdue.
6. Check customer notification delivery status so re-authorization instructions are being delivered safely.

## Safe first actions

- Use the supported customer re-authorization/replacement mandate workflow.
- Allow PAY-12 recurring and dunning state machines to progress from durable evidence only.
- Keep recurring provider calls blocked until an authorized active replacement mandate is bound.

## Forbidden actions

- Never reactivate an expired mandate by direct database update.
- Never copy or store mandate secrets, bank credentials, card data, CVV, or UPI PIN.
- Never bypass dunning/entitlement policy to hide an unresolved expired mandate.

## Escalation

Escalate when a due renewal has no valid replacement, dunning reaches restricted stages, provider mandate evidence conflicts with local state, or customer notifications repeatedly fail.

## Recovery verification

Verify a valid replacement mandate is durably authorized/active, the expired mandate remains terminal, recurring billing references the correct mandate, dunning recovers only after durable payment success, and no unauthorized entitlement was granted.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
