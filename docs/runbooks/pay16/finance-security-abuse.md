# PAY-16 runbook — Finance security abuse

## Alert and ownership

Alerts: `DoersFinanceSecurityAbuseSpike`, `DoersFinanceMakerCheckerViolation`, and `DoersFinanceSecurityVerifierUnavailable`  
Owner: `finance-security`

## Customer impact

High-risk Finance writes may be deliberately rejected while the platform protects monetary authority. A maker-checker alert indicates attempted self-approval. A revocation-verifier alert means privileged Finance writes are intentionally failing closed because session revocation truth is unavailable.

## Evidence to preserve

Preserve the structured Finance security event category, timestamp, organization/actor identifiers already present in protected logs, request/trace correlation, the immutable Finance security-audit chain for successful admitted actions, authentication/session-family evidence, and the relevant offline-payment or checkout record. Do not collect or copy access tokens, refresh tokens, webhook secrets, signatures, card data, UPI PINs, or raw provider credential payloads into incident notes.

## Safe first actions

For repeated authorization or CSRF failures, identify the affected account/session family and revoke it using the normal session-revocation path. For maker-checker attempts, leave the monetary request in its current authoritative state and route it to a different authorized checker. For a revocation-verifier outage, restore the Redis application dependency; do not weaken the PAY-16 fail-closed dependency.

Do not manually update Finance status columns, bypass maker-checker, replay provider mutations, rotate secrets without preserving the explicit current/previous overlap, or re-enable legacy payment writes.

## Escalation

Escalate immediately if a maker-checker violation occurs, if warning/critical Finance security events reach the PAY-16 five-minute threshold, if multiple organizations are affected, if an administrator token appears stolen, or if the revocation verifier remains unavailable for more than one minute.

## Recovery verification

Verify that privileged Finance requests again enforce recent authentication, cookie CSRF where applicable, current revocation truth, same-tenant authority, maker-checker and idempotency. Confirm no unauthorized payment/refund/invoice mutation occurred, the security-event metric returns to baseline, the alert clears, and the immutable audit chain still verifies sequence and previous-hash continuity.
