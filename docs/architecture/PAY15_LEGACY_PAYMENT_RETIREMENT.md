# PAY-15 — Legacy Payment Retirement and Data Migration

PAY-15 removes dual monetary authority between the historical gym-scoped member-commerce tables and Finance Core.

## Frozen predecessor

- PAY-14 SHA: `b23b81f4472554f64e08342dc1268d393d720c7f`
- PAY-14 tree: `d1ad75c713bf87b9380822081d3e36b77762255e`
- Alembic predecessor: `zz07d8e9f0a60`
- PAY-15 Alembic head: `zz17d8e9f0a61`

## Retired monetary authority

The historical tables remain available only as preserved history:

- `public.payments`
- `public.invoices`
- legacy member-subscription financial linkage

PAY-15 installs database triggers that reject INSERT, UPDATE, DELETE, and TRUNCATE against `public.payments` and `public.invoices`.

The legacy HTTP/payment service paths are also retired before they can construct or mutate a monetary row. Historical GET/report reads remain available.

Permanent dual-write is forbidden.

## Migration authority

Migration is not ordinary application authority.

Only the existing `finance_config_runtime` capability may invoke the PAY-15 `app_secure` functions. Ordinary API, auth, worker, and lifecycle-maintenance identities receive no PAY-15 migration-table DML and no PAY-15 function execution.

The controlled lifecycle is:

```text
inventory
   ↓
reconciling
   ↓
ready_for_cutover
   ↓
cutover
   ↓
rollback_hold
```

`rollback_hold` is terminal. Rollback never re-enables legacy writes.

## Record-level reconciliation

Every legacy invoice and payment must receive exactly one immutable disposition.

Invoices preserve:

- source row identity;
- organization identity;
- invoice number;
- payment reference;
- subscription reference;
- subtotal;
- discount;
- tax;
- total;
- status;
- explicit migration currency;
- source SHA-256;
- target SHA-256 when migrated.

Payments preserve:

- source row identity;
- organization identity;
- subscription reference;
- amount;
- discount;
- original status;
- transaction reference;
- Razorpay/provider reference;
- explicit migration currency;
- source SHA-256;
- target SHA-256 when migrated.

For `migrated_finance` records, the Finance target must exactly reconcile. Invoice number, invoice money/tax totals, payment amount/currency, and authoritative payment reference are not allowed to drift.

Historical records that should not become new Finance facts use `historical_read_only`. Compatibility views preserve read access without creating a second write authority.

## Subscription binding preservation

Every distinct legacy subscription referenced by a legacy payment or invoice must have one durable PAY-15 financial-link record.

The link may classify history as:

- `historical_read_only`;
- `compatibility_projection`;
- `migrated_finance`.

Ambiguous or missing linkage blocks certification. PAY-15 never guesses a modern subscription term.

## Checksums and audit

The database derives the initial source inventory SHA-256 from the actual frozen legacy rows.

Before readiness, PAY-15 recomputes:

- source row counts;
- invoice monetary total;
- invoice tax total;
- payment monetary total;
- distinct subscription-link count;
- source inventory checksum.

Each record disposition and subscription link has a deterministic checksum and append-only audit evidence.

The final manifest is derived from the complete immutable disposition/link set and is rebound again at cutover.

## Hard cutover gates

Cutover is impossible unless all are simultaneously true:

```text
legacy writes = 0
unreconciled migrated money = 0
unknown historical invoices = 0
duplicate Finance records = 0
```

Additionally:

- every source payment has a disposition;
- every source invoice has a disposition;
- every required subscription link is preserved;
- invoice totals reconcile;
- tax totals reconcile;
- payment totals reconcile;
- source inventory checksum is unchanged;
- the cutover manifest matches the certified readiness manifest.

## Rollback strategy

An empty PAY-15 migration may downgrade cleanly to PAY-14.

Once any PAY-15 migration/cutover evidence exists, downgrade fails closed and preserves the data.

Operational rollback after cutover means:

```text
Finance mutation pause
+ legacy history remains read-only
+ investigate/reconcile
+ no legacy-write reactivation
```

This avoids resurrecting dual monetary authority.

## Safety posture

- live provider activation: disabled;
- production credentials activation: disabled;
- live money movement: disabled;
- ordinary production PAY-15 migration binding: disabled;
- merge/release/deployment: not authorized by PAY-15 certification.

## Gate

`PAY15_LEGACY_RETIREMENT=PASS`
