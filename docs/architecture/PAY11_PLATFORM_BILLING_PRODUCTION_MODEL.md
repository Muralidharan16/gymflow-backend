# PAY-11 — Platform Billing Production Model

## Scope

PAY-11 completes the Gym/Studio → DOers commercial persistence model. It remains strictly separate from member subscriptions, member payments, and Finance Core member-money records.

PAY-11 is a persistence, immutability, and contract-retention phase. It does **not** authorize a live provider, production credentials, production money movement, release, merge, or deployment.

## Exact predecessor

PAY-11 is based on the immutable PAY-10 candidate:

- SHA: `4fbbde1f4fe809f433f7a4b19894c59dcaffed9b`
- tree: `ac51aff5b1c68dbec655744834ea3a070dd81ff3`
- Alembic predecessor: `zv07d8e9f0a56`
- PAY-11 Alembic head: `zw07d8e9f0a57`

## Production entities

PAY-11 introduces the required commercial records:

- `platform_provider_subscriptions`
- `platform_mandates`
- `platform_document_sequences`
- `platform_invoices`
- `platform_invoice_lines`
- `platform_payment_attempts`
- `platform_refunds`
- `platform_credit_notes`
- `platform_credit_note_lines`

It also introduces immutable commercial release persistence:

- `platform_catalog_releases`
- `platform_catalog_release_items`
- `platform_provider_releases`

The source-controlled manifests are:

- `app/platform_billing/policies/data/catalog_release_v1.json`
- `app/platform_billing/policies/data/provider_release_v1.json`

Both source manifests remain draft/non-live until separately approved commercial and provider releases are published.

## Commercial immutability

Published plan versions and active prices were already protected by the Phase-1 database triggers. PAY-11 keeps those protections and adds release-level immutability.

A catalog release may publish only when it contains at least one release item and every referenced plan is published. Every priced item must reference an active immutable price. After publication the manifest and membership are immutable; the only allowed release-row mutation is an explicit retirement transition.

A provider release binds provider code, environment, adapter version, provider contract hash, and canonical manifest hash. Published provider releases are immutable except retirement metadata.

## Subscriber contract retention

`platform_subscriptions` receives an explicit accepted commercial binding:

- accepted catalog release
- accepted provider release
- accepted plan version
- accepted price
- commercial contract SHA-256
- accepted timestamp
- controlled migration timestamp/reason

The accepted plan/price must belong to the accepted catalog release, and the current plan/price must match the accepted binding.

Once a subscription has an accepted binding, direct drift is rejected by the database. A rebind is possible only through the controlled `migrate_platform_subscription_commercial_contract(...)` capability. That operation requires a published target release, updates the binding atomically, and appends a Platform Billing audit event. Runtime application identities receive no direct mutation authority from PAY-11.

## Legal document model

Invoices and credit notes are draft-first records.

At issuance:

- an official document sequence/number is required;
- invoice/credit-note header totals must equal their line snapshots;
- issued legal fields and issued lines become immutable;
- settlement metadata may progress only through constrained status transitions;
- cumulative credit notes cannot exceed the original invoice total.

Document sequences are global seller-side legal counters and are deliberately excluded from tenant runtime DML.

## Payment and refund model

Payment attempts are tenant-bound to one invoice and one provider release. Provider references and idempotency keys are unique.

Refunds are bound to the exact succeeded payment attempt. A per-payment row lock serializes cumulative refund capacity, preventing concurrent over-refund. A succeeded refund additionally requires:

- an issued/applied credit note for the same invoice;
- provider refund reference;
- authoritative provider evidence hash.

PAY-11 does not execute provider calls. It only makes the production state model capable of representing them safely.

## Tenant isolation and authority

All tenant-owned PAY-11 tables use ENABLE + FORCE RLS with `app.current_org_id` isolation.

`app_runtime` receives read-only access to tenant-safe PAY-11 relations and immutable release metadata. PAY-11 does not grant runtime INSERT/UPDATE/DELETE authority for the new financial records.

No PAY-11 table references member subscription/payment tables.

## Downgrade safety

The PAY-11 downgrade is reversible only while the new production model is empty and no subscription has accepted a PAY-11 commercial binding.

If any PAY-11 financial/release rows or accepted commercial bindings exist, downgrade fails closed rather than deleting legal or commercial history.

## Gate

The phase is complete only when the exact-head candidate simultaneously proves:

- exact PAY-10 predecessor identity;
- linear Alembic head `zw07d8e9f0a57`;
- all required entities and ORM mappings;
- release-manifest publication immutability;
- accepted subscriber contract retention and controlled migration;
- issued invoice/credit-note immutability;
- refund capacity and credit-note backing;
- tenant RLS/read-only runtime authority;
- empty migration lifecycle reversibility;
- populated downgrade fail-closed;
- Platform Billing regressions;
- general, Finance, migration, architecture, and security regressions.

Final decision token:

`PAY11_PLATFORM_BILLING_MODEL=PASS`
