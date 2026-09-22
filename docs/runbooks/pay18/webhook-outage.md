# PAY-18 Runbook — Webhook Outage

## Alert and ownership

Alert: `DoersPay18WebhookOutage`  
Owner: `finance-reliability`  
Failure mode: `webhook_outage`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Provider events may be delayed in ingress or processing, so captured payments can remain unapplied and customer-facing billing state may lag even though provider money movement already occurred.

## Evidence to preserve

Preserve webhook ingress logs, signature-verification counters, durable inbox status/lease-fence evidence, worker/broker health, provider delivery/retry evidence, and sanitized Finance correlation. Do not copy raw bodies or signatures into tickets.

## Diagnosis

1. Determine whether the outage is ingress failure, signature rejection, durable inbox backlog, worker unavailability, or downstream payment-application backlog.
2. Check webhook_signature_failure_total separately from webhook_backlog to distinguish forgery/configuration failures from processing delay.
3. Verify provider delivery attempts and endpoint reachability without requesting or exposing webhook secrets.
4. Inspect durable inbox aggregate status and worker lease/reclaim health through approved tooling.
5. Check Redis/Celery and PostgreSQL availability because durable inbox processing depends on both delivery and database authority.
6. Check payment_application_backlog to confirm whether verified events are recorded but financial application is delayed.

## Safe first actions

- Restore ingress, worker, broker, or DB dependencies while preserving durable inbox rows.
- Allow certified claim/reclaim and idempotent replay paths to drain the inbox after the dependency is healthy.
- Rotate webhook secrets only through the approved dual-secret rotation path when compromise/configuration evidence requires it.

## Forbidden actions

- Never delete inbox rows or provider evidence to make backlog metrics green.
- Never replay raw provider mutations or bypass signature verification.
- Never paste raw webhook signatures, bodies, payment references, or credentials into logs or incident systems.

## Escalation

Escalate when backlog grows for ten minutes, verified captured events cannot be applied, dead letters appear, or signature failures indicate a possible credential incident. Include provider support when delivery attempts are absent despite healthy local ingress.

## Recovery verification

Verify new signed webhooks are accepted, backlog returns to normal, every durable inbox item is processed or explicitly reconciled, payment_application_backlog clears, no duplicate payment event/allocation exists, and signature verification remains enabled.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.
