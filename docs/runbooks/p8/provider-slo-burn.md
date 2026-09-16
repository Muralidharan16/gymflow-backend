# P8 runbook — Provider error or timeout SLO burn

## Alert and ownership

Alert: `DoersProviderSloBurn`  
Owner: `external-integrations`  
Failure mode: `provider_error_or_timeout_slo_breach`

## Customer impact

An external integration is failing or timing out faster than the allowed provider error budget. Search, notifications, storage/security calls or other provider-backed capabilities may degrade while durable DOERS obligations remain authoritative locally.

## Evidence to preserve

Preserve provider/operation/outcome metrics, circuit state, latency/error time series, rate-limit responses, provider incident/status evidence, durable command state, persisted provider references, attempt history and correlation/saga/task IDs from logs/traces.

## Diagnosis

1. Identify the bounded provider and operation driving the burn without adding tenant/request identifiers to metric labels.
2. Separate timeouts, rate limits, provider errors and open-circuit behavior.
3. Check provider status/rate-limit guidance and network/TLS reachability.
4. Inspect durable command state and determine whether any previous attempt has an ambiguous provider outcome.
5. Confirm backoff/circuit behavior is operating and not creating retry amplification.
6. Determine whether the incident is provider-wide, operation-specific, credential/configuration drift or local network failure.

## Safe first actions

- Honor provider backoff/rate-limit guidance and keep durable obligations pending/retryable.
- Allow the certified circuit/retry policy to reduce pressure while the provider is unhealthy.
- Reconcile ambiguous attempts before any replay.
- Restore approved credential/network configuration if drift is proven.

## Forbidden actions

- Never blind-retry an ambiguous external effect.
- Never bypass an open circuit or provider rate limit merely to reduce backlog.
- Never substitute a different destination, amount, tenant, object or provider reference from operator input.
- Never mark provider-backed work successful without authoritative evidence.
- Never activate deferred refund-provider execution under P8 authority.

## Escalation

Escalate when fast burn persists for 5 minutes, an open circuit remains for 10 minutes, the provider is broadly unavailable, credential compromise/drift is suspected, or any monetary/provider-ack ambiguity appears.

## Recovery verification

Verify provider success/error/latency burn returns below thresholds, circuit state closes normally, durable backlog drains under certified retry/reconciliation rules, ambiguous attempts are resolved, and no duplicate provider/business effect was created.
