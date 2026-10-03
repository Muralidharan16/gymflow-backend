# PAY-24-G — Terminal-Controlled Stage-1 Real-Money Internal Payment Canary Package

## Purpose

PAY-24-G certifies the first DOERS payment path that is technically capable of
moving real customer money, while keeping that capability unreachable from CI,
ordinary HTTP routes, and normal runtime identities.

**Certification of this package is not authorization to move money.** The real
canary has not been executed.

The eventual canary is restricted to one PAY-24-A internal organization, INR
only, and a hard maximum of INR 10.00. It requires an interactive terminal,
an exact human confirmation phrase, a previously prepared PAY-24-F live
Razorpay order, trusted deployed-release measurement, and the exact Stage-1
activation posture.

## Frozen candidate

- PAY-24-F predecessor evidence: `b72b03db0b48018dec15b7c712a9c1ca91b5639a`
- PAY-24-G application/runtime SHA: `9cef0b8572b8fc1a8345d5c6823729353bee1a34`
- application tree: `3ea670220715ce924f3bc4310e91ff1551826386`
- Alembic head: `zze7d8e9f0a74`
- passing evidence head: `8b7f05821d47ece4d702121e36b35a6ab28605ca`

## Authority chain

The eventual live terminal flow is deliberately layered:

```text
trusted deployed-release measurement
        ↓
PAY-24-A exact SHA + human Stage-1 authorization
        ↓
single internal organization
        ↓
existing PAY-24-F live Razorpay order
        ↓
PAY-24-B durable provider admission
        ↓
one-time 127.0.0.1 Razorpay Checkout page
        ↓
browser success callback
        ↓
Razorpay checkout signature verification
        ↓
read-only Razorpay GET /v1/payments/{payment_id}
        ↓
provider state must be captured=true / status=captured
        ↓
finance_reconciliation_runtime
        ↓
PAY-24-G Stage-1 DB recheck
        ↓
existing Finance provider-evidence state machine
        ↓
existing Finance payment-application gate
        ↓
PAY-24-G Stage-1 DB recheck again
        ↓
invoice allocation / ledger / finance.invoice.paid
        ↓
existing PAY-24-C/PAY-24-E entitlement authority
        ↓
explicit emergency rollback to Stage 0 after canary
```

No provider Capture API is introduced. The terminal controller does not
programmatically capture a payment or submit a refund.

## Runtime separation

Ordinary `app_runtime` remains restricted to the existing
`razorpay_sandbox` provider-evidence path.

Live `razorpay` captured-payment evidence is admitted only when the session is
a member of the isolated `finance_reconciliation_runtime` capability and the
durable PAY-24-A authority still shows the exact Stage-1 internal-organization
posture.

The generic Finance idempotency helpers remain directly executable only by
their established application authority. PAY-24-G permits the reconciliation
session to use them only as nested calls from the live-evidence SECURITY
DEFINER path:

- reservation is restricted to the exact
  `finance.provider.capture.confirm` scope;
- completion rechecks that the reserved row belongs to that exact scope;
- `finance_reconciliation_runtime` receives no direct EXECUTE grant on either
  generic idempotency helper.

The PG16 certification explicitly proves direct helper invocation from the
temporary reconciliation login is denied.

## Rollback race closure

PAY-24-G performs the durable Stage-1 check twice:

1. before live provider evidence can mutate Finance payment state to captured;
2. before a captured live payment can be allocated and posted to the invoice.

If emergency rollback changes provider egress to `closing` between those
steps, payment application fails closed.

This prevents a payment observed immediately before rollback from being
silently applied after live authority has been withdrawn.

## Terminal-only browser boundary

The real canary controller binds an ephemeral HTTP server only to
`127.0.0.1`. The callback URL contains a one-time random token and the
returned Razorpay order ID must exactly match the prepared PAY-24-F order.

A browser callback alone is insufficient. The callback signature must verify
with the live key secret and the backend must independently fetch the provider
payment and observe the exact captured state, amount, currency and order.

Any dismiss, failed checkout, mismatched order, signature failure, provider
fetch ambiguity or non-captured state prevents Finance payment application.

## Certification

P2D run `37114580173` succeeded on the frozen PAY-24-G application/runtime
SHA with evidence head `8b7f05821d47ece4d702121e36b35a6ab28605ca`.

The run proved:

- 54 static/runtime-identity contract tests passed in the initial P2D gate;
- fresh PostgreSQL 16 migration to `zze7d8e9f0a74`;
- pristine downgrade to PAY-24-F and re-upgrade to PAY-24-G;
- exact runtime-principal bindings and peer isolation;
- inherited PAY-24-C entitlement state machine: 10 tests PASS;
- inherited PAY-24-F live-order DB boundary: PASS;
- PAY-24-G real-PG16 live captured-payment authority: PASS;
- Stage 0 rejects live Razorpay evidence;
- ordinary app runtime rejects live Razorpay evidence;
- Stage 1 permits synthetic captured evidence only through the temporary
  reconciliation identity;
- reconciliation cannot directly execute the generic Finance idempotency
  helpers;
- emergency rollback/closing prevents subsequent live payment application;
- reconciliation identity cannot use the sandbox provider-evidence path;
- temporary reconciliation login is removed after the proof;
- adversarial runtime-principal drift remains fail-closed;
- final P2B/P2C runtime-role re-verification passes.

CI explicitly reported:

```text
PAY24G_MIGRATION_ROUNDTRIP=PASS
PAY24G_LIVE_PAYMENT_PG16=PASS
PAY24G_LIVE_PROVIDER_NETWORK=0
PAY24G_REAL_MONEY_MOVEMENT=0
```

## Defects exposed and repaired before PASS

Certification intentionally failed closed several times and those failures were
used to harden the package:

1. PAY-24-F's historical static test scanned the whole live-provider module,
   so PAY-24-G's new read-only payment fetch was initially mistaken for a
   PAY-24-F scope expansion. The test was narrowed to the PAY-24-F order
   client/adapter boundary.
2. PAY-24-G unit tests initially depended on pytest async plugin auto-loading,
   which P2D deliberately disables. They were converted to explicit
   `asyncio.run` execution.
3. The first PAY-24-G migration attempted its schema/function grants after
   resetting to `migration_owner`. Fresh PG16 correctly rejected that. The
   grants/revokes were moved inside the bounded `app_security_owner` window;
   migration-owner privileges were not widened.
4. An inherited PAY-24-F PG16 test asserted its old Alembic head. It was
   advanced to recognize the PAY-24-G successor head without weakening its
   behavior assertions.
5. A synthetic PG16 test parameter was incorrectly quoted, preventing the
   authority function from being reached. The fixture was corrected.
6. Live reconciliation then exposed a genuine nested-authority defect: the
   existing Finance idempotency helpers admitted only `app_runtime`. PAY-24-G
   now permits reconciliation only for the exact nested live-evidence scope,
   while preserving direct EXECUTE denial.
7. The first direct-ACL postcondition used textual function-name resolution,
   which correctly failed because `migration_owner` lacks `app_secure`
   schema USAGE. The proof now inspects `pg_proc` ACLs directly.
8. Final teardown initially tried to delete immutable payment-event history as
   the PostgreSQL admin. PAY-2 correctly rejected it. The disposable test now
   uses the reduced `migration_owner` fixture authority that PAY-2 explicitly
   preserves; the production immutability trigger remains unchanged.

## Certified posture

```text
PAY24G_TERMINAL_CANARY_PACKAGE=PASS
PAY24G_LIVE_PAYMENT_AUTHORITY=PASS
PAY24G_RECONCILIATION_RUNTIME_ISOLATION=PASS
PAY24G_SCOPED_NESTED_IDEMPOTENCY=PASS
PAY24G_MIGRATION_ROUNDTRIP=PASS
PAY24G_PG16_LIVE_PAYMENT_BOUNDARY=PASS
PAY24G_STAGE0_LIVE_EVIDENCE=DENIED
PAY24G_ROLLBACK_LIVE_APPLICATION=DENIED
PAY24G_PUBLIC_LIVE_PAYMENT_ROUTE=ABSENT
PAY24G_CAPTURE_API=ABSENT

PAY24G_LIVE_PROVIDER_NETWORK_CI=0
PAY24G_REAL_MONEY_MOVEMENT_CI=0
PAY24G_REAL_MONEY_CANARY=NOT_EXECUTED

PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```

Actual Stage-1 transition and the INR <= 10.00 real-money browser canary still
require a separate interactive terminal session and explicit human
authorization. Merging, deploying or possessing this source does not authorize
that action.
