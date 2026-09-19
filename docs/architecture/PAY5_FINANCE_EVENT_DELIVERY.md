# PAY-5 Durable Finance Event Delivery

**Certified PAY-4 predecessor:** \`bde31b620b1619c85e581933da1c8de465a6aa00\`  
**PAY-4 tree:** \`4ff5e1176657dc96643d7da01f3d7f91c564fb78\`  
**Alembic predecessor:** \`zo07d8e9f0a49\`  
**PAY-5 head:** \`zp07d8e9f0a50\`

PAY-5 completes the Finance Core → member business-domain integration for the
currently certified member-subscription paid-invoice event.

The durability contract is:

\`\`\`text
Finance transaction
    ↓
finance.outbox_events
    ↓ commit
dispatcher claims event with worker UUID + monotonic lease fence
    ↓
consumer validates live lease and tenant
    ↓
PAY-4 Finance-gated entitlement mutation
    +
product-owned consumed-event record
    ↓ atomic commit
Finance acknowledgement in a fresh transaction
\`\`\`

Delivery is at least once. Business results are effectively once.

## Product-owned consumption journal

\`public.member_subscription_finance_event_consumptions\` stores:

- organization;
- Finance event ID;
- deterministic PAY-5 idempotency key;
- Finance payload SHA-256;
- canonical subscription term ID;
- resulting term state;
- whether the first consumer transaction applied the business effect;
- consumption timestamp.

It has independent unique constraints on \`finance_event_id\` and
\`idempotency_key\`.

The journal deliberately does not create a relational FK back into the Finance
schema. Finance remains independently operable; the PAY-5 consume capability
validates the authoritative Finance event under a live lease before creating
product evidence.

## Finance outbox claim fencing

PAY-5 adds:

- \`max_attempts\`;
- \`leased_by\`;
- \`leased_until\`;
- monotonic \`lease_fence\`.

Claim uses \`FOR UPDATE SKIP LOCKED\`.

A normal pending claim increments the attempt count and lease fence. An expired
processing lease can be reclaimed without consuming another attempt, while the
lease fence increments. A stale worker/fence cannot consume, acknowledge or
release a replacement claim.

Only \`finance.invoice.paid\` events are admitted by this phase.

## Capability boundary

Worker runtime receives only EXECUTE on:

- \`claim_member_subscription_finance_events\`;
- \`consume_member_subscription_finance_event\`;
- \`acknowledge_member_subscription_finance_event\`;
- \`release_member_subscription_finance_event\`.

Worker runtime receives no direct Finance outbox table SELECT/UPDATE and no
direct product consumption-table DML.

The PAY-4 activation function remains non-directly executable by
\`worker_runtime\`. PAY-5's reduced-owner consume capability calls it only after
proving tenant and live lease/fence authority.

## Crash semantics

### Death before consumer commit

The entitlement mutation and consumed-event insert are in one transaction.
Rollback leaves neither effect. The outbox processing lease later expires and a
new worker reclaims the same attempt with a higher fence.

### Death after consumer commit, before Finance acknowledgement

The business effect and product consumption record are durable, while the
Finance event remains processing. Reclaim sees the consumption record, performs
no second business effect, and only acknowledges the Finance event.

### Duplicate delivery

A published event is not claimable again. Concurrent consumers against the same
live lease serialize on the Finance event row and converge on one consumption
record and one business mutation.

### Stale lease

Every mutation/ack/release operation requires the exact worker UUID, exact
monotonic fence and unexpired lease. ABA-style stale completion is rejected.

## Retry behavior

A retryable consumer failure releases the event back to \`pending\`. The next
normal claim increments attempt count and fence.

A deterministic failure or exhausted attempt becomes \`failed\`. Failed events
are not silently reclaimed.

If business commit succeeded but Finance acknowledgement fails, the worker does
not mark the event pending or compensate entitlement. Recovery is by lease
expiry/reclaim + product-consumption replay.

## Migration safety

PAY-5 is additive:

- no existing Finance business row rewrite;
- no provider behavior;
- no live-money enablement;
- no refund execution;
- no product entitlement backfill.

Populated downgrade fails closed when PAY-5 product-consumption evidence or
PAY-5 delivery evidence exists. Empty downgrade must restore predecessor ACL and
relation posture before re-upgrade.

## Terminal markers

\`\`\`text
PAY5_FINANCE_EVENT_DELIVERY=PASS
PAY5_PRODUCT_CONSUMPTION=PASS
PAY5_CRASH_REDELIVERY=PASS
PAY5_STALE_LEASE_FENCING=PASS
PAY5_EFFECTIVELY_ONCE=PASS
PAY5_MIGRATION_LIFECYCLE=PASS
PAY5_PAY4_INHERITED=PASS
PAY5_LIVE_MONEY_MOVEMENT=DISABLED
PAY5_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY5_FINAL=PASS
\`\`\`
