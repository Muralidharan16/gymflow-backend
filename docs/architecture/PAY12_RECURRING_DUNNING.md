# PAY-12 — Recurring Payments, Mandates and Dunning

## Scope

PAY-12 makes Platform Billing recurring renewal behavior deterministic and durable while preserving the hard separation between Gym/Studio → DOers billing and member payments.

Exact predecessor:

- PAY-11 SHA: `a9a6a0ae46aa5003f93a956a7c40cfe456c0fad6`
- PAY-11 tree: `c619ccf6dbc48e8eebe2dbe8a4706f69e4eacb0e`
- Alembic predecessor: `zw07d8e9f0a57`
- PAY-12 Alembic head: `zx07d8e9f0a58`

PAY-12 does not authorize a live provider, production credentials, production money movement, merge, release, or deployment.

## Mandates

The canonical mandate lifecycle is:

```text
pending -> authorized -> active <-> paused
   |          |          |
   +----------+----------+-> revoked
   +----------+----------+-> expired
   +----------+----------+-> failed
```

`revoked`, `expired`, and `failed` are terminal.

Supported recurring rails are modeled provider-neutrally:

- UPI AutoPay;
- e-mandate;
- recurring card, when the selected provider/account supports it.

Payment-method replacement binds the replacement mandate before the old mandate is revoked. Once a replacement binding is recorded it cannot be silently changed.

## Recurring work

Each subscription/period receives one durable `platform_recurring_billing_jobs` row. The unique subscription + service-period key prevents duplicate billing periods.

Jobs contain:

- run timestamp;
- attempt budget;
- next attempt timestamp;
- lease owner / lease expiry / monotonic fence;
- bound invoice;
- latest payment attempt;
- durable provider evidence pointer;
- terminal completion metadata.

Celery timing is not the source of truth. A delivery/retry may happen more than once, but one service period cannot create two durable recurring jobs.

## Invoice period identity

Recurring invoices persist `service_period_start` and `service_period_end`. Non-void invoices are unique for one subscription/service period.

This prevents replay, scheduler duplication, or worker crash from silently producing a second collectible invoice for the same period.

## Dunning

A dunning case can start only from durable customer/payment evidence:

- confirmed payment failure;
- mandate unavailable at collection time;
- required customer payment action.

These evidence classes are deliberately excluded:

- provider outage;
- provider timeout;
- provider outcome unknown.

Therefore:

> Provider outage alone never starts dunning and never immediately disables a valid paying customer.

Technical ambiguity stays in payment/provider-operation reconciliation. It does not become customer debt evidence.

The default versioned policy is:

```text
first confirmed failure
      ↓
3 days full access
      ↓
4 days limited write
      ↓
7 days read only
      ↓
billing only
```

A policy day is exactly 86,400 elapsed seconds.

Retry budget:

```text
max attempts: 4
spacing from first confirmed failure: 0h, 24h, 72h, 120h
```

The first attempt is the triggering attempt. Retry timestamps are persisted.

Dunning stages may move forward or recover. They cannot silently move backward.

## Late payment recovery

Authoritative late-payment success:

1. closes the dunning case as `recovered`;
2. restores subscription state to `active` when policy permits;
3. returns full access;
4. records durable payment evidence;
5. schedules a deduplicated recovery notification.

Recovery requires payment-success evidence; a support/browser assertion cannot perform it.

## Notifications

`platform_notification_deliveries` records lifecycle communications with:

- notification type;
- policy code;
- channel;
- recipient hash;
- dedupe key;
- scheduled/sent timestamps;
- attempts and safe error text.

Supported policy events include payment failure, retry scheduling, grace warning, limited/read-only restriction, suspension, recovery, mandate expiry/revocation, and payment-method replacement.

## Access engine

PAY-12 completes the `past_due` access resolver:

```text
full_grace     -> full
limited_write  -> limited_write
read_only      -> read_only
billing_only   -> billing_only
```

Access remains derived from durable state; it is not manually toggled by provider callbacks.

## Migration safety

PAY-12 is additive.

An empty PAY-12 schema may round-trip `PAY-11 -> PAY-12 -> PAY-11 -> PAY-12`.

Downgrade fails closed when any recurring jobs, dunning history, notification history, PAY-12 mandate lifecycle metadata, payment-rail migration, replacement binding, or invoice service-period identity exists.

## Certification

The exact candidate must simultaneously prove:

- mandate transition matrix and terminality;
- UPI AutoPay/e-mandate/card recurring representation;
- mandate expiry/revocation/payment-method replacement;
- duplicate-period prevention;
- provider outage non-dunning invariant;
- retry spacing and max attempts;
- exact grace-stage timing;
- durable dunning evidence;
- deduplicated customer notification model;
- late-payment recovery;
- all access stages;
- provider failure injection;
- time-advanced lifecycle simulation;
- tenant RLS and runtime read-only authority;
- PG16 migration lifecycle and populated downgrade fail-closed;
- Platform Billing, Finance, architecture/security, and migration regressions.

Gate:

`PAY12_RECURRING_DUNNING=PASS`
