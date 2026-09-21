# PAY-16 — Security and Abuse Hardening

PAY-16 assumes monetary paths are actively attacked and hardens the Finance boundary without enabling production money movement.

## Frozen predecessor

- PAY-15 SHA: 3c74c0fa888022922fd079abd0c52d1e151aaf30
- PAY-15 tree: c7c4bfeec90ba94212bca4bfe9800564e3ae9d97
- Alembic head remains: zz17d8e9f0a61
- PAY-16 adds no schema authority and does not alter PAY-15 legacy-retirement semantics.

## Threat/control mapping

| Threat | PAY-16 control |
| --- | --- |
| IDOR / cross-tenant access | Organization-bound actor identity, resource-org predicates and generic not-found responses |
| Webhook forgery | Raw-body HMAC verification before normalization |
| Signature replay | Authoritative provider event ID plus durable inbox/idempotency and future timestamp skew rejection |
| Credential theft / stolen admin token | Session-family binding, short access token, recent-auth gate and fail-closed revocation check for high-risk finance writes |
| CSRF | Access-token-JTI-bound double-submit proof for cookie-authenticated finance writes |
| Session fixation | New server-side family per login/verification and family carried through refresh rotation |
| Staff fraud | Maker-checker for offline payment approval; preparer cannot approve |
| Privilege escalation | Signed token subject/org/role must equal active server-side Staff identity |
| Refund abuse | Cumulative refund eligibility remains applied amount minus prior refunds; live provider refund remains non-public |
| Amount/currency/reference substitution | Provider evidence must reconcile to server Finance truth |
| Race / idempotency abuse | Existing command idempotency, locks and unique provider event/reference constraints |
| Provider-environment confusion | Sandbox/test-only provider configuration and live-money guards remain false |
| Log leakage | Security logs contain identifiers/reason codes only, never raw credentials/provider payloads |
| Secret rotation | Webhook verifier supports current plus one previous redacted secret |
| Invoice tampering | Existing issued-invoice immutability and Finance integrity controls remain inherited |
| Suspicious operator activity | Maker-checker and approval velocity denials emit structured security events |
| Export abuse | Secure export guard caps rows and forbids payment-authentication material; no public Finance export route exists |

Machine-readable authorization matrix:
docs/architecture/pay16_security_authorization_matrix_v1.yaml

## Privileged session contract

High-risk Finance writes require all of the following:

1. owner/admin organization-scoped principal;
2. signed token subject, organization and role equal to current server-side Staff identity;
3. authentication time no older than ten minutes;
4. current revocation evidence, failing closed if security truth is unavailable;
5. for cookie transport, matching finance_csrf cookie and X-CSRF-Token header bound to the access-token JTI.

Refresh rotation preserves original auth_time. Refreshing a stale session is not step-up authentication.

Access and refresh tokens carry the server-created session-family ID when one exists. Family revocation can therefore invalidate subsequent privileged Finance actions.

## Maker-checker and abuse velocity

Offline payment preparation and decision remain owner/admin only.

Approval additionally requires:

- checker actor different from preparer;
- bounded X-Finance-Reason-Code;
- no more than 10 successful offline approvals by the same actor in the same organization inside a rolling 5-minute window;
- existing Finance idempotency key and database monetary-state constraints.

A maker-checker or velocity denial emits a structured security event without credential/provider payload material.

The velocity limit is a security circuit breaker, not a payment amount entitlement.

## Webhook anti-forgery and anti-replay

Razorpay webhook processing continues to verify the untouched raw request body.

PAY-16 adds safe credential rotation. The configured current webhook secret is tried first and one explicit previous secret may be accepted during rotation. Both remain redacted.

Replay authority remains provider event ID plus durable inbox/idempotency state, so legitimate delayed provider retries are accepted safely rather than rejected by age alone. When the signed payload carries created_at, a timestamp more than five minutes in the future is rejected.

Provider amount, currency, order reference and payment reference never become independent mutation authority. They must reconcile to server-side Finance truth.

## Refund protection

PAY-16 does not introduce a browser/admin refund-execution route.

Existing Finance refund intent rules remain mandatory:

    eligible refund = amount applied to payment - already refunded amount

A refund above the eligible value is rejected. Provider refund requests are built from durable Finance truth rather than caller-selected payment, amount, currency or reference values.

## Payment-data minimization

DOers must not store payment-card PAN/card number, CVV/CVC, UPI PIN, or equivalent payment-authentication secrets.

Here "payment-card PAN" means a card Primary Account Number. It does not mean the 10-character Indian income-tax PAN legitimately stored for legal entities and billing parties.

The provider-hosted checkout contract exposes only the public provider key and provider order ID. PAY-16 tests scan Finance models and schemas to prevent payment-authentication fields from being introduced.

## Secure exports

PAY-16 defines a reusable secure-export guard:

- maximum 5,000 rows per request;
- PAN/card number/CVV/CVC/UPI PIN fields always forbidden.

No public Finance export route is enabled by PAY-16. A later export phase must bind this guard, recent authentication, authorization and audit before exposure.

## Certification gate

One exact PAY-16 candidate must pass:

- exact predecessor and exact scope;
- unchanged Alembic head;
- authorization matrix;
- adversarial penetration suite;
- SAST;
- dependency vulnerability audit;
- secret scan over PAY-16 application changes;
- inherited provider/refund/live-mode security contracts.

Terminal marker:

    PAY16_SECURITY_CERTIFICATION=PASS

## Safety posture

    LIVE_PROVIDER=DISABLED
    PRODUCTION_CREDENTIALS=DISABLED
    LIVE_MONEY_MOVEMENT=DISABLED
    PRODUCTION_PAY16_RUNTIME_BINDING=DISABLED
    MERGE=NOT_AUTHORIZED
    RELEASE=NOT_AUTHORIZED
    DEPLOYMENT=NOT_AUTHORIZED
