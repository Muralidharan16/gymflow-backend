# PAY-24-F — Terminal-Controlled Stage-1 Live Razorpay Order Canary

## Purpose

PAY-24-F prepares the first live-provider canary without allowing CI, normal
HTTP routes, or source-code presence to move money.

The certified operation is **Razorpay live order creation only**. Creating an
order does not capture customer money. Live webhook processing, payment
capture, refund execution and recurring billing remain outside this phase.

## Frozen candidate

- PAY-24-E predecessor evidence: `fe7d3453dd462cdafe5f2a4ebb874eeeab4bf501`
- PAY-24-F application SHA: `067b916bdb690e18510749509d441787753eb2bc`
- application tree: `a34f1a5f66bdccd39c8a144bb79d5d1abc9f441c`
- Alembic head: `zzd7d8e9f0a73`

## Authority chain

The terminal path is deliberately layered:

```text
trusted deployed-release measurement
        ↓
PAY-24-A exact SHA + human Stage-1 authorization
        ↓
single internal organization
        ↓
local Finance checkout preparation
        ↓
PAY-8 provider-operation claim
        ↓
PAY-24-B current-generation checkout admission
        ↓
commit active provider admission
        ↓
Razorpay HTTPS POST /v1/orders
        ↓
durable provider-operation result + PAY-24 admission result
        ↓
attach provider order to existing subscription checkout binding
```

The external call cannot occur before the provider admission is durably active.

## Release identity

The terminal operator cannot provide a deployed SHA argument. The controller
accepts either a protected root-owned deployment attestation or a clean Git
worktree measurement when the running deployment is literally that checkout.

The root-owned attestation must be a regular non-symlink, owned by root and not
group/other writable. This preserves the PAY-24-A rule that deployment identity
must come from a trusted measurement source rather than operator input.

## Existing HTTP behavior remains fail-closed

No live provider adapter is wired into the ordinary Finance or member
subscription HTTP routes. Those routes remain sandbox/test-only and retain
their existing PAY-24-B admission protocol.

PAY-24-F is therefore reachable only from the explicit terminal controller.

## Migration

`zzd7d8e9f0a73` widens only
`finance.provider_operations.environment` for `create_checkout` from
`sandbox/test` to `sandbox/test/live`.

It does not widen `provider_webhook_inbox`, refund, capture, settlement,
payment-application or entitlement authority.

The migration inspects the existing SECURITY DEFINER function through
`pg_proc` / `pg_namespace` catalogs. It deliberately does **not** grant
`migration_owner` USAGE on `app_secure`.

Downgrade refuses if any live provider-operation evidence exists.

## Certification

P2D run `37110908230` succeeded on the frozen application SHA.

The run proved:

- PAY-24-F static and fake-transport unit contracts;
- fresh PostgreSQL 16 migration to `zzd7d8e9f0a73`;
- pristine downgrade to PAY-24-E and re-upgrade to PAY-24-F;
- exact live runtime-principal bindings;
- inherited PAY-24-C entitlement runtime regression;
- real PG16 reservation and claim of a `live` checkout operation;
- PAY-24-A Stage 0 denies PAY-24-B provider admission for that operation;
- no provider network or live credential is present in CI;
- adversarial runtime-principal drift remains fail-closed;
- final P2B/P2C role re-verification passes.

Two certification failures were intentionally repaired before PASS:

1. the first evidence run exposed pytest plugin-autoload incompatibility in a
   test wrapper only;
2. the next run proved `migration_owner` correctly lacked `app_secure`
   USAGE, causing an unsafe name-resolution approach to fail. The repair used
   direct PostgreSQL catalog inspection instead of widening privileges.

## Terminal posture

```text
PAY24F_TRUSTED_RELEASE_MEASUREMENT=PASS
PAY24F_LIVE_ORDER_ADAPTER=PASS
PAY24F_MIGRATION_ROUNDTRIP=PASS
PAY24F_PG16_LIVE_OPERATION_BOUNDARY=PASS
PAY24F_STAGE0_PROVIDER_ADMISSION=DENIED
PAY24F_TERMINAL_INTERACTIVE_CONTROL=PASS
PAY24F_PUBLIC_LIVE_CHECKOUT_ROUTE=ABSENT

PAY24F_LIVE_PROVIDER_NETWORK_CI=0
PAY24F_REAL_MONEY_MOVEMENT=0
PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```

Actual Stage-1 transition and live Razorpay order creation require a later
interactive terminal session. No live action is authorized merely by merging,
deploying, or possessing this source.
