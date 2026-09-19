# PAY-4 Member Subscription ↔ Finance Core Integration

**Certified PAY-3 predecessor:** `4cddca720ddaac4a4f29cfc0eaf2019ecd821464`  
**PAY-3 tree:** `a4e562171d9bb3cc407063cb88bd183597318b8c`  
**Alembic predecessor:** `zn07d8e9f0a48`  
**PAY-4 head:** `zo07d8e9f0a49`

PAY-4 closes the member-entitlement authority gap. A new member admission is no
longer born active. The existing `member_subscriptions_v2` row is retained only
as a compatibility projection and is created as `pending`; the canonical
lifecycle `subscription_terms` row is created as `pending_payment`.

## Canonical immutable relationship

`finance.member_subscription_finance_bindings` persists:

- `subscription_term_id`
- `finance_invoice_id`
- `finance_payment_context_id`
- `organization_id`
- `member_id`
- immutable `plan_snapshot`
- authoritative invoice `amount`
- `currency_code`

`finance.payment_contexts` supplies a durable payment-obligation identity that
is independent of an individual provider attempt or payment row. This keeps the
binding compatible with later split-tender and offline-payment phases.

The binding and payment context are append-only outside migration authority.

## Admission and checkout

The current org-scoped admission path creates the compatibility row as
`pending`, then calls the reduced-owner capability that materializes the
canonical series/term/slot/event graph. The term is `pending_payment`.

The existing sandbox checkout path may still produce its historical P4D binding
for compatibility, but PAY-4 additionally creates the canonical term-based
Finance binding. The binding capability derives member, plan snapshot, amount
and currency from database authority; the caller cannot supply them.

## Activation authority

Only `app_secure.apply_member_subscription_finance_event(uuid,text)` can cross
PAY-4's `pending_payment → scheduled/active` gate.

It accepts only a durable Finance outbox event identity and an idempotency key,
then re-derives:

1. the exact `finance.invoice.paid` event;
2. the canonical binding and invoice;
3. invoice paid state, amount and currency;
4. all invoice allocations;
5. every allocated payment's tenant, currency and `captured/settled` state;
6. the member and plan snapshot still bound to the lifecycle term.

Frontend callback state, Razorpay JS success, raw webhook fields and API request
amount/currency are not activation authority.

A future-start term becomes `scheduled`; a term whose start date has arrived
becomes `active`. The V2 compatibility projection becomes active only when the
canonical term becomes active.

## Structural bypass prevention

Database triggers reject direct creation/transition to `active` or `scheduled`
unless the session is the migration authority or a `worker_runtime` caller is
inside the reduced `app_security_owner` activation capability.

PAY-4 intentionally leaves the activation function without production EXECUTE
for `worker_runtime`. PAY-5 owns the durable Finance-event consumer and will
bind delivery to this already-certified capability.

## Migration policy

PAY-4 is additive. It adds two Finance relations, reduced-owner policies,
functions and activation guards. It does not rewrite/backfill existing Finance
or lifecycle rows.

Downgrade fails closed if a PAY-4 binding or Finance-derived activation event
exists. Empty predecessor→head→predecessor→head lifecycle must preserve the
predecessor ACL/object posture exactly.

## Terminal markers

```text
PAY4_MEMBER_FINANCE_BINDING=PASS
PAY4_PENDING_PAYMENT_ADMISSION=PASS
PAY4_FINANCE_EVENT_ACTIVATION=PASS
PAY4_ACTIVATION_BYPASS_DENIAL=PASS
PAY4_EXACTLY_ONCE_ACTIVATION=PASS
PAY4_MIGRATION_LIFECYCLE=PASS
PAY4_PAY3_INHERITED=PASS
PAY4_LIVE_MONEY_MOVEMENT=DISABLED
PAY4_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY4_FINAL=PASS
```
