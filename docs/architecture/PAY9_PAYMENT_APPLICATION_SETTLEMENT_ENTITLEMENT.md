# PAY-9 — Payment Application, Invoice Settlement and Entitlement

## Status

Implementation candidate. PAY-9 starts from the exact certified PAY-8 SHA:

`ad0292e48bccc8b5e99c1082d6b0f7933f42789c`

PAY-8 remains inherited. PAY-9 does not authorize merge, release, deployment, live
provider credentials, live money movement, production checkout, production
webhooks, or refund-provider execution.

## Authority contract

PAY-9 preserves the separation between external money evidence, Finance
accounting truth and product entitlement:

```text
verified provider evidence
        ↓
finance.payments = captured / settled
        ↓
PAY-9 derives canonical invoice from immutable checkout binding
        ↓
PAY-4 Finance binding must match
        ↓
payment allocation
        ↓
invoice partially_paid / paid
        ↓
balanced Finance ledger posting
        ↓
Finance outbox
        ↓
PAY-5 durable consumer
        ↓
PAY-4 entitlement revalidation
        ↓
subscription active / scheduled
```

The browser, webhook body, API caller and worker cannot choose the invoice,
amount or subscription to activate. The PAY-9 capability accepts only
`payment_id` and the already-persisted verified `payment_event_id`.

## Separate states

These facts remain distinct:

- provider captured;
- Finance payment recorded;
- payment allocated;
- invoice fully paid;
- subscription entitlement activated;
- provider/bank settlement received.

A verified captured payment is sufficient for customer entitlement only after
Finance allocation fully pays the bound invoice and PAY-4/PAY-5 independently
revalidate the immutable subscription binding. Bank settlement remains later
accounting evidence and is not an entitlement prerequisite.

## Canonical allocation rule

For an eligible captured/settled payment:

```text
payment_available =
    payment.amount - sum(existing payment allocations)

invoice_outstanding =
    invoice.grand_total_amount - sum(existing invoice allocations)

amount_to_apply =
    min(payment_available, invoice_outstanding)
```

This gives the required outcomes without changing historical payment truth:

| Case | Finance result | Entitlement result |
| --- | --- | --- |
| Full payment | invoice paid | eligible after PAY-5/PAY-4 consumption |
| Underpayment | invoice partially_paid | no activation |
| Multiple payments | allocations accumulate | activates only at exact full payment |
| Split tender | offline + provider allocations coexist | activates only when total equals invoice |
| Overpayment | invoice receives only outstanding amount | excess remains unapplied credit |
| Already-paid invoice | payment remains unapplied | no duplicate effect |
| No canonical checkout binding | payment remains unapplied | no activation |
| Binding/currency mismatch | payment remains unapplied | no activation |
| Duplicate provider event | replay/no-op | no duplicate ledger or entitlement |

## Durable application evidence

`finance.payment_application_records` records the outcome of each verified
provider payment event. It is not a second ledger. Monetary authority remains:

- `finance.payments` for payment truth;
- `finance.payment_allocations` for application truth;
- `finance.invoices` for receivable state;
- `finance.ledger_entries` and `finance.ledger_entry_lines` for accounting;
- `finance.outbox_events` for durable domain delivery.

The PAY-9 record stores the decision and after-state snapshot required to explain
why captured money was applied or left unapplied.

The table uses FORCE RLS. Runtime identities receive no direct table DML.
`app_runtime` and `finance_payment_runtime` may execute only the bounded
SECURITY DEFINER capability.

## Wrong-invoice protection

There is deliberately no PAY-9 parameter for `invoice_id` or `amount`.

The capability resolves:

```text
payment_id
  → finance.member_subscription_checkout_bindings.checkout_intent_id
  → canonical invoice_id
  → finance.member_subscription_finance_bindings
```

The immutable Finance binding must agree on organization, invoice amount,
currency and Finance ownership. A caller therefore cannot substitute another
invoice, tenant, member or entitlement target.

## Accounting effect

A successful allocation posts the existing payment accounting shape:

```text
Dr PAYMENT_CLEARING
Cr AR
```

for exactly the allocated amount and emits:

- `finance.payment.applied`;
- `finance.invoice.partially_paid` or `finance.invoice.paid`;
- `finance.ledger.entry.posted`.

PAY-9 creates no second ledger and no direct subscription mutation.

## Entitlement gate

PAY-9 intentionally does not activate subscriptions itself.

The existing PAY-5 durable dispatcher consumes only the authoritative
`finance.invoice.paid` event. The PAY-4 consumer then rechecks:

- Finance binding exists;
- invoice is still `paid`;
- invoice amount/currency equals the immutable binding;
- at least one allocation exists;
- all contributing payments are `captured` or `settled`;
- allocated total exactly equals the bound invoice amount;
- subscription/member/plan snapshots have not drifted.

Only then can the pending term become `active` or `scheduled`.

## Crash and replay behavior

PAY-9 runs inside the already-durable PAY-8 webhook processing transaction:

```text
PAY-8 verified inbox
   ↓
provider evidence transition
   ↓
PAY-9 application
   ↓
PAY-8 inbox completion
   ↓
single DB commit
```

A crash before commit rolls back the provider transition, application,
ledger/outbox effects and inbox completion together; PAY-8 can reclaim the
inbox. A crash after commit sees the durable processed inbox. Exact repeated
application of the same provider event returns its original PAY-9 record.
A distinct provider event for an already-applied payment records a replay
decision and cannot duplicate allocation or ledger effects.

## Migration behavior

Revision `zs07d8e9f0a53` is the sole PAY-9 migration and directly follows
PAY-8 `zr07d8e9f0a52`.

The migration:

- creates only PAY-9-owned evidence/index/policy/capability objects;
- does not rewrite predecessor Finance tables;
- gives no runtime direct DML;
- supports empty downgrade to the exact PAY-8 predecessor;
- refuses a populated downgrade if application evidence exists.

## Certification target

PAY-9 is complete only when the exact candidate simultaneously proves:

```text
PAY9_VERIFIED_CAPTURE_GATE=PASS
PAY9_CANONICAL_ALLOCATION=PASS
PAY9_PARTIAL_PAYMENT=PASS
PAY9_SPLIT_TENDER=PASS
PAY9_OVERPAYMENT_CREDIT=PASS
PAY9_UNAPPLIED_PAYMENT=PASS
PAY9_DUPLICATE_EFFECT_FENCING=PASS
PAY9_ENTITLEMENT_GATE=PASS
PAY9_DIRECT_DML_DENIAL=PASS
PAY9_MIGRATION_LIFECYCLE=PASS
PAY9_PAY8_INHERITED=PASS
PAY9_FINAL=PASS
```
