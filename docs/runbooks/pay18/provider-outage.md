# PAY-18 Runbook — Provider Outage

## Alert and ownership

Alert: `DoersPay18ProviderOutage`  
Owner: `finance-reliability`  
Failure mode: `provider_outage`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Payment and refund provider calls may fail, time out, or become ambiguous. Local PostgreSQL financial truth remains authoritative, and customer-facing confirmation can be delayed until provider evidence is reconciled.

## Evidence to preserve

Preserve provider health/error evidence, provider-operation status and lease fence, reconciliation evidence, request/task Finance correlation, and the relevant immutable audit trail. Record timestamps and provider status-page evidence without copying secrets or payment references into incident chat.

## Diagnosis

1. Confirm the alert is driven by bounded Razorpay error, timeout, or circuit-open telemetry rather than a telemetry outage.
2. Check provider status and network reachability independently from application health; preserve timestamped evidence.
3. Inspect aggregate unknown-payment and refund-ambiguity metrics to determine whether any provider call outcome is uncertain.
4. Inspect the durable provider-operation/reconciliation state through approved Finance tooling; distinguish failed-before-send from unknown-after-send.
5. Verify worker, Redis broker, and PostgreSQL health so a provider incident is not masking an internal dependency failure.
6. Identify whether webhook ingress still works; provider API outage and webhook outage require different recovery paths.

## Safe first actions

- Keep new ambiguous provider mutations fail-closed and allow known-safe retryable commands to follow their certified policy.
- Use provider reconciliation/read APIs and durable evidence to resolve unknown outcomes before any resubmission.
- Communicate customer impact using sanitized Finance correlation and aggregate counts only.

## Forbidden actions

- Never blindly retry a provider mutation whose outcome is unknown.
- Never mark a payment or refund successful manually to clear the alert.
- Never enable live credentials, disable idempotency, or weaken lease fencing as an incident workaround.

## Escalation

Escalate to Finance reliability and the provider when the outage persists beyond five minutes, any unknown money outcome exists, or customer entitlement/invoice/refund state is blocked. Escalate to security as well if provider authentication or credential validity is in doubt.

## Recovery verification

Confirm provider success/error rates return to baseline, circuit state closes, payment/refund unknown counts stop increasing, and every ambiguous durable operation has authoritative reconciliation evidence. Verify no duplicate provider effect, no lost successful payment/refund obligation, and no manual terminal-state mutation occurred.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
