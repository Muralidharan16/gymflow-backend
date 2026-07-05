# Doers Platform Subscription V3.1
## Execution-Hardened, Agent-Safe Technical Specification

**Status:** Ready for explicit Phase 0 owner approval; implementation may begin only after approval and only phase by phase  
**Revision:** 3.1 — pre-Phase-0 concurrency, timing, environment-isolation, optimistic-concurrency, reconciliation-deduplication, and fallback hardening  
**Date:** 15 June 2026  
**Parent document:** `DOERS_PLATFORM_SUBSCRIPTION_CONSTITUTION_V2.md`  
**Scope:** Organizations paying Doers for use of the SaaS platform  
**Explicitly excluded:** Membership plans, subscriptions, payments, invoices, admissions, and collections that a facility manages for its own members  
**Target repositories:** `gymflow-backend` and `doers-frontend` from the reviewed archives  
**Primary deployment assumption:** India-first, global-capable, PostgreSQL, FastAPI, SQLAlchemy async, Alembic, React, TypeScript, React Query, Redis, Celery  

---

# 0. Authority, Relationship to V2, and Use

V2 remains the constitutional source for principles and non-negotiable boundaries. V3 converts those principles into exact implementation contracts.

When V2 and V3 appear to differ:

1. V2 wins on constitutional intent.
2. V3 wins on implementation detail where it strengthens or operationalizes V2.
3. A change that weakens tenant isolation, financial integrity, auditability, reliability, or customer recovery requires a formal architecture amendment; it cannot be introduced as an ordinary code change.

V3 deliberately improves one V2 API recommendation:

- **Tenant-facing platform-billing APIs do not accept an organization ID as ordinary authority.** They derive `organization_id` from the authenticated server-side session.
- Explicit organization IDs are used only in separately authenticated internal-control-plane APIs.

This document authorizes staged implementation. It does **not** authorize a single agent to implement all phases at once.

## 0.1 Required implementation discipline

For every phase:

1. read V2 and all relevant V3 sections;
2. inspect the current branch and migration head;
3. modify only the files authorized for that phase;
4. run all phase verification commands;
5. report files changed, migrations created, tests executed, failures, and unresolved risks;
6. stop for review;
7. commit only when explicitly instructed.

## 0.2 Production blockers that remain controlled configuration

V3 resolves architecture and implementation behavior. Two commercial release manifests remain intentionally external to code:

- `catalog_release_v1.yaml`: approved plan names, prices, currencies, limits, tax behavior, and availability;
- `provider_release_v1.yaml`: approved payment provider, account identifiers, webhook endpoints, capabilities, and operational contacts.

The foundation, access engine, UX shell, and provider-neutral contracts can be implemented before these manifests are approved. Production checkout must remain disabled until both manifests pass review.

## 0.3 Revision 3.1 pre-Phase-0 amendment

Revision 3.1 closes implementation ambiguities that could otherwise be resolved inconsistently across phases:

1. deterministic organization-scoped advisory locking before creation of a current subscription contract;
2. a frozen 150 ms synchronous access-resolution budget;
3. explicit `If-Match` requirements for every mutation route;
4. exact provider-environment matching with no test/live fallback;
5. one normalized financial-evidence application path for webhook and reconciliation repairs;
6. exact elapsed-second semantics for policy durations expressed in days;
7. a precisely defined stale-projection fallback for safe reads.

These are binding execution requirements. Phase 0 is not approved unless the corresponding runtime constants, tests, and architecture checks are present.

---

# 1. Frozen Engineering Decisions

## 1.1 Domain boundary

The bounded context is named **Platform Billing**.

```text
Backend package:       app/platform_billing/
Database prefix:       platform_
Tenant API prefix:     /api/v1/platform-billing
Webhook API prefix:    /api/v1/platform-billing/webhooks
Internal API prefix:   /api/v1/internal/platform-billing
Frontend feature:      src/features/platformBilling/
Frontend route:        /settings/plan-billing
```

Existing member-commerce tables and routes remain separate:

```text
membership_plans
member_subscriptions_v2
payments
invoices
/subscriptions
/billing
```

They must never be imported into `app/platform_billing/domain`, used as platform-billing source data, or joined to determine Doers access.

## 1.2 Source of truth

PostgreSQL is the durable correctness boundary.

- Redis may cache read projections but cannot grant access that PostgreSQL denies.
- Celery may accelerate transitions but cannot define effective state.
- The payment provider executes and reports payment operations but cannot directly authorize Doers features.
- The browser is never authoritative for organization, amount, price, tax, entitlement, status, provider object, or access mode.

## 1.3 Session model

The target browser authentication model is:

- HttpOnly, Secure authentication cookies;
- `SameSite=Lax` by default, stricter where feasible;
- short-lived access session plus rotating refresh session;
- CSRF protection on state-changing cookie-authenticated requests;
- no access or refresh token in `localStorage` or `sessionStorage`;
- recent-authentication proof for privileged billing actions.

Theme and harmless UI preferences may remain in local storage.

## 1.4 Time and money

- Canonical timestamps: PostgreSQL `TIMESTAMPTZ`, UTC.
- Display timezone: organization/user preference only.
- Money: signed `BIGINT` minor units plus ISO 4217 currency code.
- Floating-point money is forbidden.
- Every calculation uses an explicit rounding policy and records the calculation snapshot.
- Prices and invoice amounts are not inferred from frontend values.

## 1.5 Initial lifecycle policy defaults

These values are seeded as **versioned policy data**, not constants distributed through services.

### Trial policy `TRIAL-IN-V1`

```text
trial duration:                    14 days
payment method required to start: no
post-trial full-access grace:      3 days
read-only recovery window:         7 days
final mode:                        billing_only
```

### Renewal-failure policy `DUNNING-IN-V1`

```text
full-access grace:                 3 days from first confirmed failure
limited-write stage:              next 4 days
read-only stage:                   next 7 days
final mode:                        billing_only
provider retries:                  policy/provider controlled
Doers state change trigger:        durable invoice/payment evidence or reconciliation
```

### Cancellation policy `CANCEL-IN-V1`

```text
default cancellation:              end of paid period
undo allowed:                      until effective timestamp
post-cancellation read-only:       30 days
final mode:                        billing_only
customer data deletion:            separate retention workflow only
```

### Downgrade policy `DOWNGRADE-IN-V1`

```text
default effective time:            next renewal
preview required:                  yes
scheduled downgrade reversible:    yes, before effective time
over-limit behavior:               preserve data; block net-new capacity
```

These defaults require commercial approval before public launch, but implementation must use the policy-version mechanism exactly as defined.

### Duration and boundary semantics

Every policy duration expressed as a number of `days` is an **exact elapsed duration** from its triggering `TIMESTAMPTZ`:

```text
1 policy day = 86,400 elapsed seconds
N policy days = N × 86,400 elapsed seconds
```

Trial, grace, dunning, and post-cancellation day-count transitions are not aligned to UTC midnight, organization-local midnight, or any calendar-day boundary. For example, a 14-day trial starting at `2026-06-15T18:20:00Z` ends at `2026-06-29T18:20:00Z`. Organization timezone affects display only.

This rule does **not** redefine commercial `month` or `year` billing intervals as 30 or 365 days. Monthly and annual billing periods use the persisted contractual/provider calendar boundaries in `current_period_start`, `current_period_end`, and `platform_subscription_periods`. Once persisted, those UTC timestamps are authoritative.

## 1.6 Consistency model

- Contract and ledger writes are strongly consistent within PostgreSQL transactions.
- Provider state is eventually reconciled.
- Entitlement and access projections may lag their source transaction only if an outbox event is committed in the same transaction.
- Authorization uses a projection only when its source version is current; otherwise it computes from durable source rows or fails closed for privileged writes.
- Provider outages never cause automatic suspension without durable evidence that the customer is outside a valid service/grace period.

## 1.7 Numeric and boundary defaults

The following values live in one validated configuration manifest, `platform_billing_runtime_v1.yaml`. Services, workers, tests, and frontend contracts may not redefine them independently. Changing them requires architecture review, versioned configuration, and regression tests.

```yaml
access_resolution_sync_timeout_ms: 150
policy_day_seconds: 86400
provider_mapping_environment_match: exact
stale_read_fallback_minimum_restriction: read_only
stale_read_fallback_maximum_guessed_restriction: billing_only
stale_read_fallback_never_guess:
  - full
  - blocked
first_subscription_lock_namespace: platform_subscription:first_current
```

Definitions:

- `access_resolution_sync_timeout_ms` is the total wall-clock budget from detecting projection staleness until a fresh synchronous access decision is returned. At 150 ms expiry, privileged writes return `503 ACCESS_DECISION_UNAVAILABLE`.
- `provider_mapping_environment_match: exact` means `test` evidence can resolve only `test` mappings and `live` evidence can resolve only `live` mappings. No fallback, union, or second lookup across environments is permitted.
- Safe-read fallback is derived from the last resolved mode but can never guess `full` or `blocked`. The exact algorithm is defined in §8.4 and §21.6.
- The subscription advisory-lock namespace is stable across all application instances and workers.

---

# 2. Vocabulary and Naming Contract

| Term | Exact meaning |
|---|---|
| Organization | A tenant using Doers |
| Platform Billing | Organization-to-Doers commerce |
| Facility Commerce | Facility-to-member plans, subscriptions, and payments |
| Product | Commercial family, such as Doers Core |
| Plan Version | Immutable published package of entitlements |
| Price | Immutable amount/currency/interval attached to a plan version |
| Billing Account | Organization’s legal and invoicing identity |
| Subscription | Long-lived commercial contract with Doers |
| Subscription Period | Bounded trial or paid service interval |
| Subscription Change | Requested upgrade, downgrade, cancellation, or reactivation |
| Entitlement | A feature or quantitative allowance |
| Capability | An application operation evaluated against RBAC and platform access |
| Access Mode | Derived platform-wide restriction level |
| Provider Operation | Durable record of an outbound provider command |
| Webhook Inbox | Durable receipt and processing state of provider events |
| Reconciliation | Comparison and repair between provider evidence and local state |
| Override | Time-bounded, reasoned, audited exceptional access or entitlement decision |

Forbidden ambiguous customer-facing terms:

- hard lock;
- soft lock;
- membership protocol;
- subscription, without clarifying whether it is Doers plan billing or member subscription.

Preferred labels:

- **Plan & Billing** for Doers platform billing;
- **Member Subscriptions** for facility-member contracts;
- **Member Payments & Collections** for facility-member payments.

---

# 3. Required Code Structure

## 3.1 Backend

```text
app/platform_billing/
├── __init__.py
├── api/
│   ├── tenant.py
│   ├── webhooks.py
│   ├── internal.py
│   └── schemas.py
├── domain/
│   ├── enums.py
│   ├── errors.py
│   ├── money.py
│   ├── state_machine.py
│   ├── access_decision.py
│   ├── entitlement_resolver.py
│   ├── commands.py
│   └── events.py
├── models/
│   ├── catalog.py
│   ├── billing_account.py
│   ├── subscription.py
│   ├── entitlement.py
│   ├── ledger.py
│   ├── provider.py
│   ├── webhook.py
│   ├── reconciliation.py
│   └── audit.py
├── repositories/
│   ├── catalog.py
│   ├── billing_accounts.py
│   ├── subscriptions.py
│   ├── entitlements.py
│   ├── ledger.py
│   ├── provider.py
│   └── webhooks.py
├── services/
│   ├── query_service.py
│   ├── command_service.py
│   ├── checkout_service.py
│   ├── webhook_service.py
│   ├── reconciliation_service.py
│   ├── invoice_service.py
│   ├── dunning_service.py
│   └── notification_service.py
├── providers/
│   ├── base.py
│   ├── registry.py
│   ├── fake.py
│   └── <approved_provider>.py
├── policies/
│   ├── capabilities.py
│   ├── entitlements.py
│   ├── route_manifest.py
│   └── policy_loader.py
├── tasks/
│   ├── webhook_processor.py
│   ├── reconciliation.py
│   ├── lifecycle_tick.py
│   └── notifications.py
└── observability/
    ├── metrics.py
    ├── queries.py
    └── health.py
```

Do not put platform-billing logic into the existing member subscription/payment services.

## 3.2 Frontend

```text
src/features/platformBilling/
├── api/
│   ├── platformBillingApi.ts
│   └── contracts.ts
├── components/
│   ├── AccountStatusBanner.tsx
│   ├── CurrentPlanCard.tsx
│   ├── PlanComparison.tsx
│   ├── PlanChangePreview.tsx
│   ├── PaymentMethodCard.tsx
│   ├── InvoiceList.tsx
│   ├── BillingAccountForm.tsx
│   ├── PendingConfirmationPanel.tsx
│   └── RestrictedActionNotice.tsx
├── hooks/
│   ├── usePlatformBillingSummary.ts
│   ├── usePlanChangePreview.ts
│   ├── useCheckoutSession.ts
│   └── usePlatformCapability.ts
├── pages/
│   ├── PlanBillingPage.tsx
│   ├── CheckoutReturnPage.tsx
│   └── BillingRecoveryPage.tsx
├── state/
│   └── preservedDrafts.ts
└── index.ts
```

Global access-mode rendering belongs in an application-shell provider, not scattered interceptors.

## 3.3 Machine-readable policy files

The repository must contain version-controlled definitions:

```text
app/platform_billing/policies/data/capabilities_v1.yaml
app/platform_billing/policies/data/entitlements_v1.yaml
app/platform_billing/policies/data/access_matrix_v1.yaml
app/platform_billing/policies/data/lifecycle_policies_v1.yaml
app/platform_billing/policies/data/platform_billing_runtime_v1.yaml
```

The service validates these files at startup and in CI. Production commercial catalogue data is loaded only from an approved release manifest.

---

# 4. PostgreSQL Conventions and Safety Rules

## 4.1 General columns

Unless explicitly stated otherwise, tenant-owned tables include:

```text
id                UUID PRIMARY KEY DEFAULT gen_random_uuid()
organization_id   UUID NOT NULL
created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
updated_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
version           BIGINT NOT NULL DEFAULT 1
```

Mutable aggregate roots use optimistic versioning. Updates use:

```sql
UPDATE ...
SET ..., version = version + 1
WHERE id = :id
  AND organization_id = :organization_id
  AND version = :expected_version;
```

Zero updated rows is a domain version conflict. A tenant HTTP mutation protected by `If-Match` maps it to `412 PRECONDITION_FAILED`; an internal/background command without an HTTP precondition maps it to `409 RESOURCE_VERSION_CONFLICT`.

## 4.2 Tenant foreign keys

Every tenant aggregate root has:

```sql
UNIQUE (id, organization_id)
```

Every tenant child uses a composite foreign key:

```sql
FOREIGN KEY (parent_id, organization_id)
REFERENCES parent_table (id, organization_id)
ON DELETE RESTRICT
```

Financial, provider, subscription-event, and audit rows never cascade-delete with the organization.

## 4.3 State columns

State columns use lowercase `TEXT` plus named `CHECK` constraints and matching Python `str, Enum` definitions. Native PostgreSQL enum types are not used for V3 platform billing because state evolution and zero-downtime rollouts require additive check-constraint migrations.

## 4.4 Immutability

Published catalogue records, issued financial documents, successful payment facts, provider event identity, and append-only events are protected by database triggers.

The trigger may allow only narrowly defined operational columns, such as:

- processing status;
- delivery attempts;
- last error;
- processed timestamp;
- storage pointer populated after durable document generation.

Business fields remain immutable.

## 4.5 Deletion

- Catalogue drafts may be deleted before publication when unreferenced.
- Published catalogue rows are retired, not deleted.
- Billing accounts are closed, not deleted.
- Subscriptions are ended, not deleted.
- Invoices, payments, refunds, credits, webhook receipts, and audits are never hard-deleted through tenant APIs.
- Retention purges, where legally allowed, require a dedicated maintenance workflow, legal retention evaluation, immutable purge audit, and backup policy coordination.

## 4.6 RLS

All tenant-owned platform tables enable and force RLS.

Tenant policy shape:

```sql
USING (
  organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
)
WITH CHECK (
  organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
)
```

The application role must be `NOBYPASSRLS` and must not own the protected tables.

Background workers do not receive unrestricted tenant access. They:

1. resolve an exact organization through a narrowly scoped provider-mapping function or queued event;
2. set `SET LOCAL app.current_org_id`;
3. process one tenant transaction;
4. clear context through transaction completion.

A dedicated SECURITY DEFINER lookup function may resolve provider object IDs to an organization. It must:

- use a fixed `search_path`;
- accept `provider`, `environment`, `provider_object_type`, and `provider_object_id` as explicit parameters;
- derive and validate `environment` from the configured webhook endpoint and verified signing-secret context, never solely from an untrusted payload field;
- require the exact predicate `provider = :provider AND environment = :environment AND provider_object_id = :provider_object_id`;
- reject zero-match, multi-match, and cross-environment-match cases;
- never retry the lookup against another environment, even when the same provider object ID exists there;
- return the minimum fields;
- be executable only by the billing processor role;
- write a security audit record for anomalous or ambiguous mappings.

A `test` webhook therefore cannot resolve a `live` provider mapping, and a `live` webhook cannot resolve a `test` mapping, even when provider object IDs collide.

## 4.7 Organization deletion protection

Before organization deletion or anonymization, a database guard checks for retained platform billing records. The ordinary organization delete path must fail with `409 RETAINED_FINANCIAL_RECORDS` and direct the caller to the controlled retention workflow.

---

# 5. Exact Data Model

The schema is introduced in stages. “Required in Phase” identifies the earliest migration that may create the table.

## 5.1 Catalogue

### 5.1.1 `platform_products` — Phase 1

Global, non-tenant catalogue root.

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| code | VARCHAR(40) | uppercase canonical code, unique |
| name | VARCHAR(120) | non-empty |
| description | TEXT | nullable |
| status | TEXT | `draft`, `active`, `retired` |
| created_by | UUID | internal actor, nullable for migration |
| created_at | TIMESTAMPTZ | required |
| updated_at | TIMESTAMPTZ | required |

Constraints:

- `code = upper(code)`;
- active/retired products cannot be deleted;
- product code is immutable after first plan publication.

### 5.1.2 `platform_policy_versions` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| code | VARCHAR(60) | unique, e.g. `TRIAL-IN-V1` |
| policy_type | TEXT | `trial`, `dunning`, `cancellation`, `downgrade`, `refund`, `retention` |
| version | INTEGER | `> 0` |
| payload | JSONB | schema-validated |
| status | TEXT | `draft`, `published`, `retired` |
| payload_sha256 | CHAR(64) | required at publish |
| published_at | TIMESTAMPTZ | nullable until publish |
| created_by | UUID | internal actor |
| created_at | TIMESTAMPTZ | required |

Unique: `(policy_type, version)` and `code`.

Published payload is immutable.

### 5.1.3 `platform_plan_versions` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| product_id | UUID | FK product, RESTRICT |
| version | INTEGER | `> 0` |
| code | VARCHAR(60) | unique canonical external code |
| display_name | VARCHAR(120) | required |
| description | TEXT | nullable |
| status | TEXT | `draft`, `published`, `retired` |
| trial_policy_version_id | UUID | FK policy, nullable |
| dunning_policy_version_id | UUID | FK policy, required before publication |
| cancellation_policy_version_id | UUID | FK policy, required before publication |
| downgrade_policy_version_id | UUID | FK policy, required before publication |
| metadata_json | JSONB | non-authoritative display metadata only |
| published_at | TIMESTAMPTZ | nullable until publication |
| retired_at | TIMESTAMPTZ | nullable |
| created_by | UUID | internal actor |
| created_at | TIMESTAMPTZ | required |
| updated_at | TIMESTAMPTZ | required |

Unique: `(product_id, version)`.

Published rows are immutable except `status -> retired` and `retired_at`.

### 5.1.4 `platform_prices` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| plan_version_id | UUID | FK, RESTRICT |
| code | VARCHAR(80) | unique |
| currency_code | CHAR(3) | uppercase ISO code |
| country_code | CHAR(2) | nullable availability scope |
| billing_interval | TEXT | `month`, `year`, `one_time` |
| interval_count | SMALLINT | `> 0`; one-time must equal 1 |
| amount_minor | BIGINT | `>= 0` |
| tax_behavior | TEXT | `exclusive`, `inclusive`, `not_applicable` |
| status | TEXT | `draft`, `active`, `retired` |
| valid_from | TIMESTAMPTZ | required for active price |
| valid_until | TIMESTAMPTZ | nullable, greater than `valid_from` |
| provider_price_hint | VARCHAR(120) | nullable, never authoritative |
| published_at | TIMESTAMPTZ | nullable |
| created_by | UUID | internal actor |
| created_at | TIMESTAMPTZ | required |

Active/published price fields are immutable. A price change creates a new row.

### 5.1.5 `platform_feature_definitions` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| key | VARCHAR(120) | unique dot-separated key |
| display_name | VARCHAR(120) | required |
| value_type | TEXT | `boolean`, `integer`, `string`, `json` |
| enforcement_mode | TEXT | `hard`, `soft`, `metered`, `informational` |
| unit | VARCHAR(40) | nullable |
| description | TEXT | required |
| status | TEXT | `active`, `retired` |
| created_at | TIMESTAMPTZ | required |

Keys are immutable and never reused for a different meaning.

### 5.1.6 `platform_plan_entitlements` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| plan_version_id | UUID | FK, RESTRICT |
| feature_definition_id | UUID | FK, RESTRICT |
| value_type | TEXT | copied from definition |
| value_boolean | BOOLEAN | nullable |
| value_integer | BIGINT | nullable |
| value_string | TEXT | nullable |
| value_json | JSONB | nullable |
| created_at | TIMESTAMPTZ | required |

Unique: `(plan_version_id, feature_definition_id)`.

Check: exactly one value column is populated and it matches `value_type`. A validation trigger confirms type agreement with the feature definition.

Published plan entitlement rows are immutable.

## 5.2 Billing identity and provider mappings

### 5.2.1 `platform_billing_accounts` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | unique active account per org |
| status | TEXT | `active`, `closed` |
| legal_name | VARCHAR(200) | required |
| billing_email | VARCHAR(320) | normalized and required |
| billing_phone_e164 | VARCHAR(20) | nullable |
| country_code | CHAR(2) | required |
| default_currency_code | CHAR(3) | required |
| address_line1 | TEXT | encrypted at application boundary where required |
| address_line2 | TEXT | nullable/encrypted |
| city | VARCHAR(120) | required |
| subdivision | VARCHAR(120) | nullable |
| postal_code | VARCHAR(32) | nullable by country |
| tax_registration_type | VARCHAR(30) | nullable |
| tax_registration_encrypted | TEXT | nullable |
| tax_registration_masked | VARCHAR(40) | nullable |
| tax_registration_hash | CHAR(64) | nullable keyed hash for uniqueness/search |
| tax_verified | BOOLEAN | default false |
| tax_verified_at | TIMESTAMPTZ | nullable |
| invoice_locale | VARCHAR(20) | default `en-IN` |
| created_by | UUID | actor |
| updated_by | UUID | actor |
| created_at/updated_at/version | standard | required |

Do not copy or reuse the member-payment billing identity automatically. Organization data may prefill a form, but the customer confirms the billing account snapshot.

### 5.2.2 `platform_provider_customers` — Phase 4

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| billing_account_id | UUID | composite FK |
| provider | VARCHAR(40) | canonical provider key |
| environment | TEXT | `test`, `live` |
| provider_customer_id | VARCHAR(180) | required |
| provider_created_at | TIMESTAMPTZ | nullable |
| metadata_json | JSONB | minimal non-secret metadata |
| status | TEXT | `active`, `superseded` |
| created_at/updated_at/version | standard | required |

Unique:

- `(provider, environment, provider_customer_id)`;
- one active mapping per `(billing_account_id, provider, environment)`.

### 5.2.3 `platform_payment_methods` — Phase 4

Metadata only; no card number, CVV, UPI PIN, bank credential, or mandate secret.

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| billing_account_id | UUID | composite FK |
| provider_customer_mapping_id | UUID | composite FK |
| provider | VARCHAR(40) | required |
| environment | TEXT | test/live |
| provider_payment_method_id | VARCHAR(180) | required |
| method_type | TEXT | `card`, `upi`, `bank_mandate`, `other` |
| display_brand | VARCHAR(60) | nullable |
| display_last4 | CHAR(4) | nullable |
| expiry_month | SMALLINT | nullable 1..12 |
| expiry_year | SMALLINT | nullable |
| status | TEXT | `pending`, `active`, `requires_action`, `expired`, `revoked` |
| is_default | BOOLEAN | required default false |
| provider_fingerprint_hash | CHAR(64) | nullable keyed hash |
| created_at/updated_at/version | standard | required |

Partial unique index ensures one default active payment method per billing account.

### 5.2.4 `platform_mandates` — Phase 5

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| payment_method_id | UUID | composite FK |
| provider_mandate_id | VARCHAR(180) | required |
| status | TEXT | `pending`, `active`, `paused`, `revoked`, `expired`, `failed` |
| authorized_amount_minor | BIGINT | nullable, `>= 0` |
| currency_code | CHAR(3) | nullable |
| authorized_at | TIMESTAMPTZ | nullable |
| expires_at | TIMESTAMPTZ | nullable |
| revoked_at | TIMESTAMPTZ | nullable |
| metadata_json | JSONB | non-secret |
| created_at/updated_at/version | standard | required |

Unique provider mandate identity per environment/provider.

## 5.3 Subscription contract

### 5.3.1 `platform_subscriptions` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| billing_account_id | UUID | composite FK |
| status | TEXT | defined in §7 |
| current_plan_version_id | UUID | FK global catalogue |
| current_price_id | UUID | FK global price, nullable during no-price trial |
| policy_snapshot_json | JSONB | exact policy IDs and critical values |
| started_at | TIMESTAMPTZ | required |
| current_period_start | TIMESTAMPTZ | required |
| current_period_end | TIMESTAMPTZ | required, greater than start |
| cancel_at_period_end | BOOLEAN | default false |
| cancellation_requested_at | TIMESTAMPTZ | nullable |
| cancellation_effective_at | TIMESTAMPTZ | nullable |
| canceled_at | TIMESTAMPTZ | nullable |
| ended_at | TIMESTAMPTZ | nullable |
| provider_subscription_mapping_id | UUID | nullable, Phase 4 |
| created_by | UUID | actor/system |
| updated_by | UUID | actor/system |
| created_at/updated_at/version | standard | required |

Partial unique index permits at most one current contract per organization where status is one of:

```text
trialing, active, past_due, pause_scheduled, paused, cancel_scheduled
```

Before any command inserts a row that could become the first/current contract for an organization, the transaction must serialize creation with:

```sql
SELECT pg_advisory_xact_lock(
  hashtextextended(
    'platform_subscription:first_current:' || CAST(:organization_id AS text),
    0
  )
);
```

After acquiring the transaction-scoped lock, the command must re-query the organization’s current-contract rows, apply idempotency/replay rules, and only then insert. The partial unique index remains the final database invariant. Advisory-lock hash collisions may cause harmless extra serialization but cannot weaken correctness.

Historical `canceled` and `expired` contracts remain.

Check constraints enforce timestamp/status consistency.

### 5.3.2 `platform_subscription_items` — Phase 1

Supports base plan and future add-ons.

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| subscription_id | UUID | composite FK |
| item_type | TEXT | `base_plan`, `addon` |
| plan_version_id | UUID | FK global |
| price_id | UUID | FK global, nullable for trial |
| quantity | INTEGER | `> 0` |
| effective_from | TIMESTAMPTZ | required |
| effective_until | TIMESTAMPTZ | nullable |
| status | TEXT | `scheduled`, `active`, `ended` |
| created_at/updated_at/version | standard | required |

Exactly one active `base_plan` item per current subscription.

### 5.3.3 `platform_subscription_periods` — Phase 1

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| subscription_id | UUID | composite FK |
| period_type | TEXT | `trial`, `paid`, `grace`, `extension`, `post_cancel_read_only` |
| status | TEXT | `scheduled`, `open`, `closed`, `void` |
| starts_at | TIMESTAMPTZ | required |
| ends_at | TIMESTAMPTZ | required, greater than start |
| source_invoice_id | UUID | nullable, Phase 5 |
| source_change_id | UUID | nullable |
| source_override_id | UUID | nullable |
| metadata_json | JSONB | reason/source snapshot |
| created_at | TIMESTAMPTZ | required |

Use a GiST exclusion constraint on `(subscription_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)` for non-void periods that are mutually exclusive. Any deliberately overlapping extension must first close/replace the affected period in one transaction.

### 5.3.4 `platform_subscription_changes` — Phase 2

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| subscription_id | UUID | composite FK |
| change_type | TEXT | `upgrade`, `downgrade`, `cancel`, `undo_cancel`, `pause`, `resume`, `reactivate` |
| status | TEXT | defined in §7 |
| from_plan_version_id | UUID | nullable |
| to_plan_version_id | UUID | nullable |
| from_price_id | UUID | nullable |
| to_price_id | UUID | nullable |
| requested_effective_at | TIMESTAMPTZ | required |
| actual_effective_at | TIMESTAMPTZ | nullable |
| preview_snapshot_json | JSONB | required for customer plan/cancel changes |
| request_idempotency_key | VARCHAR(160) | required |
| request_hash | CHAR(64) | required |
| expected_subscription_version | BIGINT | required |
| requested_by | UUID | actor |
| canceled_by | UUID | nullable |
| failure_code | VARCHAR(80) | nullable |
| failure_detail_safe | TEXT | nullable, no secrets |
| created_at/updated_at/version | standard | required |

Unique `(organization_id, request_idempotency_key)`.

### 5.3.5 `platform_subscription_events` — Phase 1

Append-only domain history.

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| subscription_id | UUID | composite FK |
| sequence_number | BIGINT | strictly increasing per subscription |
| event_type | VARCHAR(100) | canonical event name |
| occurred_at | TIMESTAMPTZ | business occurrence |
| recorded_at | TIMESTAMPTZ | database receipt |
| actor_type | TEXT | `user`, `system`, `provider`, `support` |
| actor_id | UUID | nullable |
| source_type | TEXT | `command`, `webhook`, `reconciliation`, `scheduler`, `migration` |
| source_id | UUID | nullable |
| evidence_sha256 | CHAR(64) | nullable canonical normalized-provider-fact identity |
| payload_json | JSONB | redacted domain data |
| payload_sha256 | CHAR(64) | integrity hash |

Unique `(subscription_id, sequence_number)`. Add:

- a partial unique constraint on `(subscription_id, source_type, source_id, event_type)` where `source_id IS NOT NULL`, preventing repeat application of the same source record;
- a partial unique constraint on `(subscription_id, evidence_sha256, event_type)` where `evidence_sha256 IS NOT NULL`, preventing webhook and reconciliation paths from applying the same normalized provider fact under different source records.

`evidence_sha256` is computed by the provider adapter from canonical normalized evidence: provider, environment, object type, provider object ID, normalized fact type/status, amount, currency, linked invoice/subscription identity, provider fact version or effective timestamp, and the immutable fields required by that fact type. Webhook processing and reconciliation must produce the same digest for the same financial fact.

No update/delete permission for application roles.

### 5.3.6 `platform_access_overrides` — Phase 2

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| override_type | TEXT | `access_mode`, `entitlement` |
| capability_or_feature_key | VARCHAR(120) | nullable for whole access mode |
| value_json | JSONB | required |
| reason_code | VARCHAR(80) | required |
| reason_detail | TEXT | required |
| starts_at | TIMESTAMPTZ | required |
| expires_at | TIMESTAMPTZ | required, greater than start |
| status | TEXT | `scheduled`, `active`, `expired`, `revoked` |
| requested_by | UUID | internal actor |
| approved_by | UUID | separate actor for privileged thresholds |
| revoked_by | UUID | nullable |
| ticket_reference | VARCHAR(120) | required |
| created_at/updated_at/version | standard | required |

Constraints:

- requester cannot approve when four-eyes policy applies;
- no non-expiring override;
- maximum normal duration 7 days;
- longer override requires elevated approval and explicit expiry no later than 30 days;
- overrides never alter invoice/payment facts.

## 5.4 Entitlement and access projections

### 5.4.1 `platform_entitlement_projection` — Phase 2

One resolved row per organization and feature.

| Column | Type | Rules |
|---|---|---|
| organization_id | UUID | PK part |
| feature_key | VARCHAR(120) | PK part |
| value_type | TEXT | required |
| value_boolean/integer/string/json | typed value | exactly one |
| source_plan_version_id | UUID | nullable |
| source_override_id | UUID | nullable |
| effective_from | TIMESTAMPTZ | required |
| effective_until | TIMESTAMPTZ | nullable |
| source_subscription_version | BIGINT | required |
| resolution_version | BIGINT | required |
| resolved_at | TIMESTAMPTZ | required |
| input_sha256 | CHAR(64) | required |

Projection rows are replaceable, but every replacement emits an outbox/audit event.

### 5.4.2 `platform_access_projection` — Phase 2

Exactly one row per organization.

| Column | Type | Rules |
|---|---|---|
| organization_id | UUID | PK |
| subscription_id | UUID | nullable |
| mode | TEXT | `full`, `limited_write`, `read_only`, `billing_only`, `blocked` |
| reason_code | VARCHAR(80) | required |
| reason_detail_safe | TEXT | customer-safe summary |
| effective_from | TIMESTAMPTZ | required |
| next_transition_at | TIMESTAMPTZ | nullable |
| recovery_actions_json | JSONB | server-authored actions |
| source_subscription_version | BIGINT | nullable |
| resolution_version | BIGINT | required |
| resolved_at | TIMESTAMPTZ | required |
| input_sha256 | CHAR(64) | required |

### 5.4.3 `platform_usage_projection` — Phase 2

Display/reconciliation snapshot only. Capacity enforcement must count or reserve against authoritative domain rows within the write transaction.

| Column | Type | Rules |
|---|---|---|
| organization_id | UUID | PK part |
| metric_key | VARCHAR(120) | PK part |
| current_value | BIGINT | `>= 0` |
| measured_at | TIMESTAMPTZ | required |
| source_high_watermark | VARCHAR(160) | nullable |
| stale_after | TIMESTAMPTZ | required |

## 5.5 Financial ledger

### 5.5.1 `platform_document_sequences` — Phase 5

| Column | Type | Rules |
|---|---|---|
| legal_entity_code | VARCHAR(40) | PK part |
| fiscal_period | VARCHAR(20) | PK part |
| document_type | TEXT | PK part: `invoice`, `credit_note` |
| next_value | BIGINT | `> 0` |
| updated_at | TIMESTAMPTZ | required |

Number allocation uses `SELECT ... FOR UPDATE` in the same transaction that issues the document. Gaps may occur after rollback only if law/accounting policy permits; otherwise use a dedicated issue transaction and void record. Final numbering policy requires finance/legal approval.

### 5.5.2 `platform_invoices` — Phase 5

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| billing_account_id | UUID | composite FK |
| subscription_id | UUID | composite FK |
| invoice_number | VARCHAR(80) | nullable in draft, unique once issued |
| status | TEXT | `draft`, `open`, `paid`, `void`, `uncollectible` |
| invoice_type | TEXT | `tax_invoice`, `bill_of_supply`, `proforma`, `other` |
| currency_code | CHAR(3) | required |
| subtotal_minor | BIGINT | required |
| discount_total_minor | BIGINT | required default 0 |
| tax_total_minor | BIGINT | required default 0 |
| total_minor | BIGINT | required |
| amount_paid_minor | BIGINT | required default 0 |
| amount_due_minor | BIGINT | required |
| service_period_start/end | TIMESTAMPTZ | required |
| issued_at | TIMESTAMPTZ | nullable until issue |
| due_at | TIMESTAMPTZ | nullable |
| paid_at | TIMESTAMPTZ | nullable |
| voided_at | TIMESTAMPTZ | nullable |
| billing_identity_snapshot_json | JSONB | required before issue |
| seller_identity_snapshot_json | JSONB | required before issue |
| tax_calculation_snapshot_json | JSONB | required before issue |
| provider_invoice_id | VARCHAR(180) | nullable |
| document_storage_key | TEXT | nullable |
| document_sha256 | CHAR(64) | nullable |
| created_at/updated_at/version | standard | required |

Checks recompute arithmetic:

```text
subtotal - discount + tax = total
amount_paid + amount_due = total, subject to credits/refunds represented explicitly
all amounts use same currency
```

Issued invoice business fields are immutable.

### 5.5.3 `platform_invoice_lines` — Phase 5

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| invoice_id | UUID | composite FK |
| line_number | INTEGER | `> 0` |
| line_type | TEXT | `plan`, `addon`, `discount`, `tax`, `adjustment` |
| description | TEXT | required |
| quantity | NUMERIC(18,6) | required, non-negative |
| unit_amount_minor | BIGINT | required |
| net_amount_minor | BIGINT | required |
| tax_amount_minor | BIGINT | required |
| gross_amount_minor | BIGINT | required |
| service_period_start/end | TIMESTAMPTZ | nullable |
| plan_version_id | UUID | nullable |
| price_id | UUID | nullable |
| tax_snapshot_json | JSONB | required where applicable |
| created_at | TIMESTAMPTZ | required |

Unique `(invoice_id, line_number)`. Immutable after invoice issue.

### 5.5.4 `platform_payment_attempts` — Phase 5

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| invoice_id | UUID | composite FK |
| payment_method_id | UUID | nullable composite FK |
| provider | VARCHAR(40) | required |
| environment | TEXT | test/live |
| provider_payment_id | VARCHAR(180) | nullable until created |
| attempt_number | INTEGER | `> 0` |
| status | TEXT | defined in §7 |
| amount_minor | BIGINT | `> 0` |
| currency_code | CHAR(3) | required |
| requires_action_type | VARCHAR(80) | nullable |
| failure_code | VARCHAR(100) | nullable normalized code |
| failure_detail_safe | TEXT | nullable |
| provider_created_at | TIMESTAMPTZ | nullable |
| succeeded_at | TIMESTAMPTZ | nullable |
| failed_at | TIMESTAMPTZ | nullable |
| raw_provider_reference | JSONB | minimal identifiers, no secret payload |
| created_at/updated_at/version | standard | required |

Provider payment identity is unique when present.

### 5.5.5 `platform_refunds` — Phase 5

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| payment_attempt_id | UUID | composite FK |
| invoice_id | UUID | composite FK |
| provider_refund_id | VARCHAR(180) | nullable |
| status | TEXT | `requested`, `processing`, `succeeded`, `failed`, `canceled` |
| amount_minor | BIGINT | `> 0` |
| currency_code | CHAR(3) | required |
| reason_code | VARCHAR(80) | required |
| reason_detail | TEXT | internal, restricted |
| requested_by | UUID | actor |
| approved_by | UUID | nullable/required by threshold |
| succeeded_at | TIMESTAMPTZ | nullable |
| created_at/updated_at/version | standard | required |

Aggregate successful refunds cannot exceed captured payment amount minus previous successful refunds.

### 5.5.6 `platform_credit_notes` and `platform_credit_note_lines` — Phase 5

Mirror immutable invoice-document principles. Each credit note references an issued invoice, carries its own sequence number, reason, tax snapshot, lines, totals, issue timestamp, storage hash, and status `draft`, `issued`, `void`.

A credit note does not imply cash refund. Refund linkage is explicit.

## 5.6 Provider reliability

### 5.6.1 `platform_provider_operations` — Phase 4

Durable saga record for outbound provider calls.

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| organization_id | UUID | tenant |
| operation_type | TEXT | `create_customer`, `create_checkout`, `create_subscription`, `change_subscription`, `cancel_subscription`, `setup_payment_method`, `refund`, `fetch_object` |
| provider | VARCHAR(40) | required |
| environment | TEXT | test/live |
| idempotency_key | VARCHAR(180) | required |
| request_hash | CHAR(64) | required |
| status | TEXT | `reserved`, `in_flight`, `succeeded`, `failed_retryable`, `failed_final`, `unknown` |
| provider_object_type | VARCHAR(80) | nullable |
| provider_object_id | VARCHAR(180) | nullable |
| request_snapshot_json | JSONB | redacted canonical request |
| response_snapshot_json | JSONB | redacted normalized response |
| attempt_count | INTEGER | `>= 0` |
| next_attempt_at | TIMESTAMPTZ | nullable |
| last_error_code | VARCHAR(100) | nullable |
| last_error_safe | TEXT | nullable |
| lease_owner | UUID | nullable |
| lease_until | TIMESTAMPTZ | nullable |
| created_at/updated_at/version | standard | required |

Unique `(provider, environment, idempotency_key)`.

Never blindly retry status `unknown`; reconcile by idempotency key/provider lookup first.

### 5.6.2 `platform_webhook_inbox` — Phase 4

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| provider | VARCHAR(40) | required |
| environment | TEXT | test/live |
| provider_event_id | VARCHAR(200) | required |
| event_type_raw | VARCHAR(160) | nullable before parse |
| event_type_normalized | VARCHAR(120) | nullable |
| provider_created_at | TIMESTAMPTZ | nullable |
| received_at | TIMESTAMPTZ | required |
| signature_verified | BOOLEAN | required |
| payload_storage_key | TEXT | encrypted object storage pointer or encrypted DB blob |
| payload_sha256 | CHAR(64) | required |
| headers_redacted_json | JSONB | minimal |
| status | TEXT | `received`, `processing`, `processed`, `ignored`, `retry`, `dead_letter` |
| organization_id | UUID | nullable until safe mapping |
| processing_attempts | INTEGER | `>= 0` |
| next_attempt_at | TIMESTAMPTZ | nullable |
| lease_owner | UUID | nullable |
| lease_until | TIMESTAMPTZ | nullable |
| processed_at | TIMESTAMPTZ | nullable |
| last_error_code | VARCHAR(100) | nullable |
| last_error_safe | TEXT | nullable |

Unique `(provider, environment, provider_event_id)`.

Provider event ID and payload hash are immutable.

### 5.6.3 `platform_reconciliation_runs` — Phase 4

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| provider/environment | text | required |
| scope | TEXT | `recent_events`, `subscriptions`, `invoices`, `payments`, `refunds`, `full_sample` |
| status | TEXT | `running`, `completed`, `completed_with_errors`, `failed` |
| cursor_json | JSONB | continuation state |
| started_at/completed_at | TIMESTAMPTZ | required/nullable |
| scanned_count/mismatch_count/repaired_count/error_count | BIGINT | default 0 |
| initiated_by | UUID | nullable system/internal actor |
| summary_json | JSONB | redacted |

### 5.6.4 `platform_reconciliation_items` — Phase 4

| Column | Type | Rules |
|---|---|---|
| id | UUID | PK |
| run_id | UUID | FK |
| organization_id | UUID | nullable until mapping |
| object_type | VARCHAR(80) | required |
| local_object_id | UUID | nullable |
| provider_object_id | VARCHAR(180) | nullable |
| mismatch_type | VARCHAR(100) | required |
| severity | TEXT | `info`, `warning`, `critical` |
| status | TEXT | `detected`, `auto_repaired`, `manual_review`, `resolved`, `ignored` |
| evidence_json | JSONB | redacted normalized comparison |
| repair_action | VARCHAR(100) | nullable |
| created_at/resolved_at | TIMESTAMPTZ | required/nullable |
| resolved_by | UUID | nullable |

## 5.7 Audit and notifications

### 5.7.1 `platform_billing_audit_events` — Phase 1

Append-only, partitionable by `recorded_at`.

Fields:

```text
id, recorded_at, organization_id, actor_type, actor_id,
action, target_type, target_id, request_id, correlation_id,
ip_hash, user_agent_hash, before_hash, after_hash,
metadata_redacted_json, outcome, reason_code
```

Audit rows are not customer financial documents and must not contain secrets or raw sensitive provider payloads.

### 5.7.2 `platform_notification_deliveries` — Phase 3

Tracks customer communications and frequency limiting:

```text
id, organization_id, notification_type, policy_version_id,
channel, recipient_hash, status, scheduled_at, sent_at,
provider_message_id, dedupe_key, attempt_count, last_error_safe
```

Unique dedupe keys prevent repeated banners/emails for the same lifecycle event.

---

# 6. Catalogue Publication and Configuration Protocol

## 6.1 Draft validation

Before a plan version can be published:

- all referenced policy versions are published;
- at least one valid price exists unless the plan is trial-only/internal;
- every entitlement key exists and value type matches;
- no duplicate entitlement keys;
- required baseline features exist;
- country/currency combinations are coherent;
- price amount and interval are valid;
- plan display metadata passes schema validation;
- a deterministic manifest hash is produced.

## 6.2 Publication transaction

Publication is one database transaction that:

1. locks the draft plan version;
2. validates catalogue graph;
3. writes manifest SHA-256;
4. changes status to published;
5. timestamps publication;
6. emits `platform.catalog.plan_version_published` to the transactional outbox;
7. writes an immutable audit event.

The transaction performs no provider API call.

## 6.3 Retirement

Retirement prevents new purchase. It does not mutate existing subscriptions. Existing contracts continue on their pinned plan and price unless a separately approved migration is scheduled.

## 6.4 Commercial release manifest

An approved manifest contains no secret values and must be reviewed by product, finance, and engineering.

Example shape:

```yaml
release: DOERS-IN-2026-01
country: IN
currency: INR
plans:
  - plan_code: DOERS_STARTER_V1
    monthly_price_minor: <approved value>
    annual_price_minor: <approved value>
    entitlements:
      limits.branches.active: <approved value>
      limits.members.active: <approved value>
      features.attendance: true
approval:
  product: <reference>
  finance: <reference>
  engineering: <reference>
```

Placeholders are rejected by the production loader.

---

# 7. State Machines

State transitions are validated in the domain layer and persisted under row lock. Direct arbitrary status assignment is forbidden.

## 7.1 Subscription status

Allowed statuses:

```text
trialing
active
past_due
pause_scheduled
paused
cancel_scheduled
canceled
expired
```

Allowed transitions:

| From | To | Trigger | Required evidence |
|---|---|---|---|
| none | trialing | onboarding provision | published trial policy + selected eligible plan |
| none | active | paid subscription provision | confirmed provider/local payment evidence |
| trialing | active | conversion confirmed | paid period created; provider state confirmed |
| trialing | expired | trial/recovery policy elapsed | authoritative clock + no paid period |
| trialing | canceled | account closure | explicit authorized command |
| active | past_due | renewal payment failure | open invoice + confirmed failed/overdue evidence |
| active | cancel_scheduled | customer cancellation | preview + recent auth + expected version |
| cancel_scheduled | active | undo cancellation | before effective time |
| cancel_scheduled | canceled | period end | lifecycle evaluation |
| active | pause_scheduled | supported commercial policy | approved command/provider capability |
| pause_scheduled | active | undo pause | before effective time |
| pause_scheduled | paused | effective time | lifecycle evaluation/provider confirmation where needed |
| paused | active | resume confirmed | new paid period or provider confirmation |
| paused | cancel_scheduled | customer cancellation | authorized command |
| paused | canceled | immediate approved termination | authorized command |
| past_due | active | payment recovery | invoice paid/valid paid period |
| past_due | cancel_scheduled | customer schedules closure | policy permits; no avoidance of owed invoices |
| past_due | canceled | approved termination | period/policy elapsed or internal action |

Forbidden:

- `canceled -> active` on the same contract;
- `expired -> active` on the same contract;
- any state mutation from a frontend redirect;
- any state mutation based only on an unsigned/unverified webhook payload.

Reactivation after terminal state creates a new subscription contract linked in event metadata.

## 7.2 Subscription change status

```text
requested
validated
provider_pending
scheduled
applied
canceled
failed_retryable
failed_final
```

Allowed transition pattern:

```text
requested -> validated -> provider_pending -> scheduled/applied
requested/validated -> failed_final
provider_pending -> failed_retryable -> provider_pending
scheduled -> applied
scheduled -> canceled
```

Every transition appends a subscription event and increments aggregate version.

## 7.3 Payment attempt status

```text
created
requires_customer_action
processing
succeeded
failed
canceled
partially_refunded
refunded
```

Allowed transitions:

- `created -> requires_customer_action | processing | succeeded | failed | canceled`
- `requires_customer_action -> processing | succeeded | failed | canceled`
- `processing -> succeeded | failed | canceled`
- `succeeded -> partially_refunded | refunded`
- `partially_refunded -> partially_refunded | refunded`

Terminal facts cannot revert. A provider correction is represented through reconciliation evidence and an explicit corrective event, never silent status rewriting.

## 7.4 Invoice status

```text
draft -> open -> paid
             -> uncollectible
             -> void
```

- `draft -> void` is allowed.
- `paid` is terminal except explicit credit/refund documents.
- `void` cannot be paid.
- `uncollectible` may become paid only if finance policy permits and payment is later confirmed; the event history must preserve both facts.

## 7.5 Webhook processing status

```text
received -> processing -> processed
                       -> ignored
                       -> retry -> processing
                       -> dead_letter
```

Lease expiry may return `processing -> retry`. It cannot create duplicate domain effects because source-event dedupe and aggregate locking remain active.

## 7.6 Access mode is derived, not manually transitioned

Access modes:

```text
full
limited_write
read_only
billing_only
blocked
```

The access resolver recomputes mode from durable inputs. Ordinary code never writes a mode without recording the resolver input hash and resolution version.

---

# 8. Access Decision Engine

## 8.1 Inputs

The resolver receives one immutable input object:

```text
organization identity/status
current subscription and version
current subscription periods
current time from database clock
invoice/payment/dunning evidence
published policy snapshots
active access overrides
security/compliance restriction
entitlement projection source version
```

Use `SELECT clock_timestamp()` from PostgreSQL for lifecycle boundary evaluation in write transactions. Application time may be used for display only.

## 8.2 Priority order

Highest priority first:

1. confirmed security/compliance block requiring product denial;
2. active explicitly approved access override, subject to override scope;
3. organization legally/administratively closed;
4. valid trial or paid service period;
5. dunning policy stage;
6. post-cancellation read-only period;
7. no current service period;
8. inconsistent state safety fallback.

Inconsistent state fallback:

- permit Plan & Billing, invoices, support, logout, and security settings;
- deny privileged product writes;
- permit safe reads only when tenant isolation and data consistency remain guaranteed;
- emit a critical reconciliation alert.

## 8.3 Deterministic algorithm

```python
def resolve_access(inputs, now):
    if inputs.security_block.active:
        return blocked(reason=inputs.security_block.reason)

    if inputs.override.active and inputs.override.scope == "access_mode":
        return bounded_override(inputs.override)

    if inputs.organization_closed:
        return billing_only("ORGANIZATION_CLOSED")

    if inputs.has_valid_trial_period(now):
        return full("TRIAL_ACTIVE", next_transition=trial_end)

    if inputs.has_valid_paid_period(now):
        if inputs.subscription.status == "past_due":
            return resolve_dunning_stage(inputs, now)
        return full("PAID_PERIOD_ACTIVE", next_transition=period_end)

    if inputs.within_trial_full_grace(now):
        return full("TRIAL_GRACE", next_transition=grace_end)

    if inputs.within_dunning_full_grace(now):
        return full("PAYMENT_GRACE", next_transition=stage_end)

    if inputs.within_dunning_limited_write(now):
        return limited_write("PAYMENT_OVERDUE", next_transition=stage_end)

    if inputs.within_any_read_only_window(now):
        return read_only(inputs.read_only_reason, next_transition=stage_end)

    if inputs.state_inconsistent:
        return safe_recovery_mode("BILLING_STATE_REVIEW_REQUIRED")

    return billing_only("NO_ACTIVE_SERVICE_PERIOD")
```

## 8.4 Projection freshness

For every request, the capability guard compares:

```text
platform_access_projection.source_subscription_version
against
platform_subscriptions.version
```

Behavior:

- equal: projection is eligible;
- projection behind and operation is read-only: attempt synchronous recomputation within `access_resolution_sync_timeout_ms = 150`; if it cannot complete, apply only the safe-read fallback below;
- projection behind and operation is write/financial/admin: synchronously recompute within the same 150 ms total wall-clock budget; if unavailable, deny with `503 ACCESS_DECISION_UNAVAILABLE`, never grant by stale cache;
- projection ahead: critical integrity error; deny privileged actions and alert.

Safe-read fallback is allowed only for capabilities classified `read`, excluding exports, security-sensitive reads, financial mutations, admin operations, and any endpoint whose resource authorization cannot be established independently. Before fallback, the guard checks durable security-block state through its dedicated minimal query. A confirmed active security block returns `blocked` and is not a fallback guess. Otherwise normalize the last resolved projection as follows:

```text
last full          -> read_only
last limited_write -> read_only
last read_only     -> read_only
last billing_only  -> billing_only
last blocked without freshly confirmed durable block -> billing_only
no last decision   -> read_only
```

Thus fallback is the more restrictive of the usable last-known mode and `read_only`, capped at `billing_only`; it never guesses `full` or `blocked`.

Redis may cache the projection with its source version. A cache entry is ignored if versions differ.

## 8.5 Recovery actions

The server returns normalized actions, never arbitrary URLs from provider payloads:

```text
VIEW_PLAN_BILLING
UPDATE_PAYMENT_METHOD
COMPLETE_PAYMENT_ACTION
DOWNLOAD_INVOICES
CONTACT_SUPPORT
EXPORT_DATA
UNDO_CANCELLATION
```

The frontend maps action identifiers to trusted internal routes.

---

# 9. Entitlement Registry

## 9.1 Initial feature definitions

Quantitative limits:

```text
limits.branches.active               integer hard
limits.members.active                integer hard
limits.staff.active                  integer hard
limits.membership_plans.active       integer hard
limits.storage.bytes                 integer hard
limits.monthly_messages              integer metered
limits.api_requests.monthly          integer metered
retention.audit_days                 integer informational
```

Boolean features:

```text
features.multi_branch                boolean hard
features.attendance                  boolean hard
features.member_subscriptions        boolean hard
features.member_payments             boolean hard
features.basic_reports               boolean hard
features.advanced_reports            boolean hard
features.custom_branding             boolean hard
features.data_export                 boolean hard
features.api_access                  boolean hard
features.whatsapp                    boolean hard
features.priority_support            boolean informational
```

Keys may be added, but meaning and value type of an existing key cannot change.

## 9.2 Resolution order

For each feature:

1. active, approved entitlement-specific override;
2. active base-plan entitlement;
3. active add-on entitlement composition;
4. feature default, which is deny/zero unless explicitly designated safe.

Composition rules are defined per feature:

- limits normally sum base and additive add-ons;
- boolean features use logical OR;
- restrictive compliance overrides may force false/lower values;
- no generic JSON merge without a registered resolver.

## 9.3 Capacity enforcement

Every capacity-increasing write must use a domain-specific transactional guard.

Example for branch creation:

1. authenticate actor and tenant;
2. evaluate RBAC permission;
3. evaluate platform capability and access mode;
4. lock the organization’s entitlement guard using `pg_advisory_xact_lock(hash(org_id, feature_key))` or a dedicated counter row;
5. read current entitlement from a fresh/current projection;
6. count authoritative active branches within the same transaction or use a transactionally maintained counter;
7. reject if `current >= limit`;
8. create the branch;
9. update usage/counter and outbox atomically;
10. commit.

Two simultaneous creates cannot both consume the final slot.

## 9.4 Over-limit downgrade behavior

When current usage exceeds the new limit:

- preserve all existing records;
- do not randomly deactivate resources;
- mark the metric `over_limit` in the billing summary;
- permit view, export, normal use of already active records according to access mode;
- permit actions that reduce usage;
- block actions that increase the over-limit metric;
- show exact current value and allowed value;
- provide upgrade or remediation actions.

---

# 10. Capability Registry and Enforcement Matrix

RBAC and platform access are independent gates:

```text
allow = authenticated
        AND tenant_match
        AND RBAC_allows
        AND platform_capability_allows
        AND resource_state_allows
```

## 10.1 Initial capability keys

Always/recovery capabilities:

```text
auth.session.refresh
auth.logout
security.manage_own_session
support.contact
platform_billing.view
platform_billing.manage_account
platform_billing.manage_payment_method
platform_billing.change_plan
platform_billing.cancel
platform_billing.download_invoice
data.export
```

Product capabilities:

```text
organization.view
organization.update
branches.view
branches.create
branches.update
branches.change_status
branches.delete_request
branch_contacts.view
branch_contacts.manage
branch_hours.view
branch_hours.manage
staff.view
staff.invite
staff.update
staff.revoke
members.view
members.create
members.update
members.deactivate
membership_plans.view
membership_plans.create
membership_plans.update
membership_plans.archive
member_subscriptions.view
member_subscriptions.create
member_subscriptions.update
member_subscriptions.cancel
member_payments.view
member_payments.record
member_payments.refund
attendance.view
attendance.record
reports.basic.view
reports.advanced.view
imports.create
imports.view
assets.view
assets.manage
```

Internal capabilities:

```text
internal.platform_billing.view
internal.platform_billing.reconcile
internal.platform_billing.issue_refund
internal.platform_billing.apply_credit
internal.platform_billing.override_access
internal.platform_billing.manage_catalog
internal.platform_billing.view_sensitive_audit
```

## 10.2 Access-mode matrix

Legend: A = allowed subject to RBAC/entitlement; D = denied; C = conditionally allowed.

| Capability group | full | limited_write | read_only | billing_only | blocked |
|---|---:|---:|---:|---:|---:|
| auth refresh/logout | A | A | A | A | A |
| own security settings | A | A | A | A | C |
| Plan & Billing view | A | A | A | A | C |
| update payment method | A | A | A | A | C |
| download platform invoices | A | A | A | A | C |
| contact support | A | A | A | A | A |
| export data | A | A | A | C | C |
| product reads | A | A | A | D | D |
| normal updates to existing records | A | C | D | D | D |
| attendance recording | A | C | D | D | D |
| capacity-increasing creates | A | D | D | D | D |
| destructive actions | A | C | D | D | D |
| plan change/cancel | A | A | A | A | C |
| internal operations | separate internal policy | separate | separate | separate | separate |

Conditional decisions are declared per capability in `access_matrix_v1.yaml`; they are not improvised in controllers.

Examples in `limited_write`:

- update a member phone number: allowed;
- record attendance for an existing active member: allowed by default policy;
- create another member or branch: denied;
- delete financial/member history: denied regardless of plan.

## 10.3 Universal route classification

Create a `CapabilityAPIRoute`/`CapabilityAPIRouter` abstraction that requires each tenant route to declare:

```text
capability key
operation class: read | modify_existing | increase_capacity | destructive | financial | recovery
resource scope resolver
```

At application startup and in CI:

- enumerate all non-public routes;
- fail if a tenant route lacks a capability declaration;
- fail if a capability key is unknown;
- fail if a public route is accidentally marked tenant-authenticated or vice versa;
- emit an auditable route-capability manifest.

Temporary migration exemptions must be explicit, expire by date, and fail CI after expiry.

HTTP method alone is never used as the access decision.

---

# 11. Tenant API Contract

Base path:

```text
/api/v1/platform-billing
```

The authenticated session supplies tenant and actor. Any `organization_id` in request body is rejected as an unknown/forbidden field.

## 11.1 Read endpoints

```text
GET /summary
GET /plans?country_code=IN&currency_code=INR
GET /subscription
GET /invoices?cursor=&limit=
GET /invoices/{invoice_id}
GET /invoices/{invoice_id}/document
GET /payment-methods
GET /billing-account
GET /plan-changes/{change_id}
```

## 11.2 Mutation endpoints

All financial/commercial POST/PATCH requests require:

```text
Idempotency-Key: 16..160 characters
X-CSRF-Token: required for cookie-authenticated state-changing requests
```

Existing aggregates use strong opaque ETags. Read/create responses expose, as applicable:

```text
ETag: "subscription:{subscription_id}:{version}"
ETag: "billing-account:{billing_account_id}:{version}"
ETag: "checkout-session:{checkout_session_id}:{version}"
```

The client must echo the exact ETag in `If-Match`. Missing `If-Match` returns `428 PRECONDITION_REQUIRED`; a stale or mismatched ETag returns `412 PRECONDITION_FAILED`. Idempotency replay still returns the originally committed response when the same key/hash has already completed.

| Route | Existing aggregate referenced | `If-Match` rule |
|---|---|---|
| `PATCH /billing-account` | billing account | **required**: billing-account ETag |
| `POST /payment-method-setup-sessions` | billing account | **required**: billing-account ETag |
| `POST /checkout-sessions` | none for first purchase, or current subscription for change/renewal | optional only when the server confirms no current subscription exists; otherwise **required**: subscription ETag |
| `POST /plan-change-previews` | current subscription | **required**: subscription ETag |
| `POST /plan-changes` | current subscription | **required**: subscription ETag; must also equal the preview source subscription version |
| `POST /cancellation-schedules` | current subscription | **required**: subscription ETag |
| `POST /cancellation-schedules/undo` | current subscription | **required**: subscription ETag |
| `POST /payment-confirmation-refresh` | checkout session/provider operation | **required**: checkout-session ETag |

A request may not downgrade an existing-aggregate route into a pure-creation route by omitting an identifier or body field. The server determines aggregate existence from authenticated tenant state.

`payment-confirmation-refresh` schedules/runs a bounded reconciliation lookup. It does not trust the browser to declare success and is strictly rate-limited.

## 11.3 Summary response

```json
{
  "schema_version": 1,
  "access": {
    "mode": "full",
    "reason_code": "TRIAL_ACTIVE",
    "effective_from": "2026-06-15T10:00:00Z",
    "next_transition_at": "2026-06-29T10:00:00Z",
    "recovery_actions": ["VIEW_PLAN_BILLING"]
  },
  "subscription": {
    "id": "uuid",
    "status": "trialing",
    "version": 3,
    "plan": {"code": "...", "display_name": "..."},
    "period_start": "...",
    "period_end": "...",
    "cancel_at_period_end": false
  },
  "usage": [
    {"key": "limits.branches.active", "current": 1, "limit": 3, "over_limit": false}
  ],
  "billing_account_complete": true,
  "payment_action": null,
  "server_time": "2026-06-15T10:00:00Z"
}
```

No provider secret or raw provider error is returned.

## 11.4 Checkout response

The endpoint returns `202 Accepted` when provider confirmation remains pending:

```json
{
  "checkout_session_id": "internal-uuid",
  "status": "ready",
  "redirect_url": "provider-hosted-approved-url",
  "expires_at": "...",
  "confirmation_state": "not_started"
}
```

Redirect URL validation:

- generated only by approved provider adapter;
- HTTPS required outside local development;
- hostname must match provider allowlist;
- never accepted from customer request.

## 11.5 Structured errors

Use RFC 9457-style problem details:

```json
{
  "type": "https://errors.doers.app/platform-billing/access-restricted",
  "title": "This action is currently unavailable",
  "status": 403,
  "code": "PLATFORM_ACCESS_RESTRICTED",
  "detail": "Your account is in read-only mode while billing is resolved.",
  "instance": "/api/v1/members",
  "request_id": "uuid",
  "access_mode": "read_only",
  "reason_code": "PAYMENT_OVERDUE",
  "next_transition_at": "...",
  "recovery_actions": ["UPDATE_PAYMENT_METHOD", "CONTACT_SUPPORT"]
}
```

Canonical error codes:

```text
PLATFORM_ACCESS_RESTRICTED            403
PLATFORM_FEATURE_NOT_INCLUDED        403
ENTITLEMENT_LIMIT_REACHED             409
PRECONDITION_REQUIRED                 428
PRECONDITION_FAILED                   412
RESOURCE_VERSION_CONFLICT             409 (internal/background commands)
IDEMPOTENCY_KEY_REQUIRED              400
IDEMPOTENCY_REQUEST_CONFLICT          409
BILLING_ACCOUNT_INCOMPLETE            422
PLAN_NOT_AVAILABLE                    422
PLAN_CHANGE_PREVIEW_EXPIRED           409
PAYMENT_CONFIRMATION_PENDING          202
PROVIDER_TEMPORARILY_UNAVAILABLE      503
ACCESS_DECISION_UNAVAILABLE           503
RECENT_AUTHENTICATION_REQUIRED        401/403
MFA_REQUIRED                          403
RETAINED_FINANCIAL_RECORDS            409
```

A `403` must never trigger an unconditional global redirect. The frontend uses the structured mode and recovery actions.

---

# 12. Internal Control Plane API

Base path:

```text
/api/v1/internal/platform-billing/organizations/{organization_id}
```

These routes require:

- separate internal audience/session;
- named internal capability;
- MFA;
- recent authentication;
- ticket/reference field;
- immutable audit;
- four-eyes approval for configured thresholds.

Routes may include:

```text
GET  /diagnostics
POST /reconciliation-requests
POST /refund-requests
POST /credit-note-requests
POST /access-overrides
POST /access-overrides/{id}/approve
POST /access-overrides/{id}/revoke
GET  /audit-events
```

Internal actors cannot edit raw statuses directly. They submit domain commands through the same state machine.

No general “set subscription active” endpoint exists.

---

# 13. Provider Adapter Contract

## 13.1 Interface

```python
class PlatformBillingProvider(Protocol):
    key: str

    async def create_customer(self, command: CreateCustomer) -> ProviderCustomerResult: ...
    async def create_checkout_session(self, command: CreateCheckout) -> CheckoutResult: ...
    async def create_payment_method_setup(self, command: SetupPaymentMethod) -> SetupResult: ...
    async def change_subscription(self, command: ChangeProviderSubscription) -> ChangeResult: ...
    async def cancel_subscription(self, command: CancelProviderSubscription) -> CancelResult: ...
    async def refund_payment(self, command: RefundProviderPayment) -> RefundResult: ...
    async def fetch_subscription(self, reference: ProviderReference) -> ProviderSubscriptionSnapshot: ...
    async def fetch_invoice(self, reference: ProviderReference) -> ProviderInvoiceSnapshot: ...
    async def fetch_payment(self, reference: ProviderReference) -> ProviderPaymentSnapshot: ...
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> VerifiedWebhook: ...
    def normalize_webhook(self, verified: VerifiedWebhook) -> NormalizedProviderEvent: ...
```

## 13.2 Normalized results

Adapters return provider-neutral DTOs. Domain services never branch on raw event names or provider response shapes.

Normalized event types:

```text
customer.created
checkout.completed
subscription.created
subscription.updated
subscription.canceled
invoice.opened
invoice.paid
invoice.payment_failed
payment.requires_action
payment.succeeded
payment.failed
refund.succeeded
refund.failed
mandate.updated
dispute.opened
dispute.closed
```

## 13.3 Provider capability declaration

Every adapter declares:

```text
hosted_checkout
recurring_payments
payment_method_setup
webhook_signatures
provider_idempotency
subscription_scheduling
proration_preview
refunds
partial_refunds
invoice_objects
mandates
upi_autopay_or_equivalent
test_clock_or_sandbox_time_control
```

Unsupported capability causes a deterministic domain error or a documented fallback; services do not assume parity.

## 13.4 Secrets

Provider secrets are loaded from a secret manager/environment injection and represented by opaque configuration objects.

Forbidden:

- secret in source control;
- secret in database row;
- secret in task payload;
- raw authorization header in log;
- full webhook payload in ordinary application logs.

## 13.5 Fake provider

Phase 4 must implement a deterministic fake provider before any real adapter. It supports:

- success;
- customer action required;
- retryable timeout before/after remote creation;
- permanent failure;
- delayed webhook;
- duplicate webhook;
- out-of-order webhook;
- missing webhook followed by reconciliation;
- refund success/failure.

The fake provider is the primary integration-test harness.

---

# 14. Command, Idempotency, and Remote-Call Protocol

## 14.1 Required idempotency

Required for:

```text
checkout creation
payment-method setup
plan change
cancellation/undo
reactivation
refund
credit application
manual payment recording
access override
customer/provider creation
webhook domain application
reconciliation repair
```

## 14.2 Request identity

Idempotency scope:

```text
organization_id + operation_name + idempotency_key
```

Canonical request hash includes only authoritative normalized request fields. It excludes volatile headers, tracing IDs, and display-only data.

Reuse behavior:

- same key + same hash + completed: replay stored response;
- same key + different hash: `409 IDEMPOTENCY_REQUEST_CONFLICT`;
- same key + in progress: wait bounded time or return `202` with operation reference;
- stale in-progress with provider result unknown: reconcile before retry;
- failed final: replay stable failure unless caller uses a new key after correcting input.

## 14.3 Existing Doers idempotency engine

The existing DB-backed `IdempotencyEngine` is the starting foundation, but platform billing must harden integration by:

- checking stored request hash on loser/replay paths;
- scoping keys by operation as well as tenant;
- never treating the Redis middleware as financial correctness;
- storing/replaying responses without relying on unavailable external blob storage;
- defining safe unknown-remote-result recovery;
- preventing zombie reclamation from issuing a duplicate provider operation;
- testing key retention and archival against dispute/retry windows.

Do not use only `IdempotencyMiddleware` for billing mutations.

## 14.4 Outbound provider saga

Never hold a database transaction open while waiting on an external provider.

### Transaction A — reserve

1. validate command and expected version;
2. acquire application idempotency;
3. create/resume `platform_provider_operations` row;
4. persist pending domain command/change;
5. commit.

### External step

1. mark leased/in-flight;
2. call provider with provider idempotency key;
3. receive success, known failure, or unknown outcome.

### Transaction B — record

1. lock provider operation and affected aggregate;
2. persist normalized result;
3. update command/change state as allowed;
4. do **not** activate paid service from browser redirect;
5. emit outbox/audit;
6. commit.

Unknown outcome enters `unknown` and reconciliation. It is never blindly retried as a new provider command.

## 14.5 Transactional outbox

Use the existing `transactional_outbox` only after verifying its constraints and poller behavior. Platform billing events use namespaced event types and deterministic dedupe keys:

```text
platform.billing.subscription.changed
platform.billing.access.changed
platform.billing.entitlements.changed
platform.billing.invoice.issued
platform.billing.payment.succeeded
platform.billing.notification.requested
```

The outbox event is inserted in the same transaction as the domain change.

---

# 15. Webhook Acceptance and Processing

## 15.1 HTTP acceptance flow

1. enforce provider-specific body-size limit before parsing;
2. read exact raw bytes once;
3. verify signature/timestamp using adapter;
4. reject invalid signature with `400`/provider-compatible response;
5. derive provider event ID and payload hash;
6. encrypt/store payload and insert inbox row in one durable operation;
7. on duplicate event ID, compare payload hash:
   - same hash: acknowledge duplicate;
   - different hash: record critical security anomaly and do not process automatically;
8. return success acknowledgement only after durable insert/duplicate recognition;
9. do not run domain processing in the request transaction.

## 15.2 Processing flow

Worker query:

```sql
SELECT ...
FROM platform_webhook_inbox
WHERE status IN ('received', 'retry')
  AND next_attempt_at <= clock_timestamp()
  AND (lease_until IS NULL OR lease_until < clock_timestamp())
ORDER BY received_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

For each event:

1. lease row;
2. decrypt/load payload;
3. normalize through adapter;
4. map the provider object to the exact internal organization through the protected lookup using `provider = :provider AND environment = :environment AND provider_object_id = :provider_object_id`; reject cross-environment fallback or collision;
5. set tenant DB context;
6. lock affected subscription/invoice/payment aggregate;
7. check source-event dedupe;
8. compare event evidence with current durable state;
9. apply allowed idempotent transition or mark ignored;
10. append domain event and audit;
11. update projections/outbox in same transaction;
12. mark inbox processed.

## 15.3 Out-of-order events

Provider creation time is evidence, not a universal ordering guarantee.

When an event could regress state:

- fetch the current provider object through a provider operation;
- compare normalized object version/timestamps and local terminal facts;
- apply only monotonic/corrective transitions;
- record ignored stale evidence;
- reconcile uncertain cases.

A late `payment.processing` event cannot regress a locally confirmed `payment.succeeded` fact.

## 15.4 Retry policy

- exponential backoff with jitter;
- bounded attempts before dead letter;
- retry only normalized retryable errors;
- validation, signature, unmapped-object, and invariant failures route to manual review according to category;
- dead letters trigger alerts and remain replayable through controlled tooling.

## 15.5 Payload retention

Raw provider payload retention is minimized and encrypted. Parsed normalized business facts remain in domain tables. Retention period is determined by legal/security policy and provider dispute windows.

---

# 16. Reconciliation Protocol

Reconciliation is mandatory, not optional observability.

## 16.1 Schedules

Recommended initial cadence:

```text
recent checkout/payment unknowns:      every 5 minutes
recent subscriptions/invoices:         hourly
all active subscriptions:              daily rolling scan
refunds/disputes:                      hourly/daily by provider capability
sample historical financial objects:   weekly
```

Cadence is configuration and may be tuned without changing domain semantics.

## 16.2 Comparison rules

Reconciliation compares provider snapshots to local:

- customer mapping;
- subscription identity/status/current period;
- invoice total/currency/status;
- payment amount/currency/status;
- refund amount/status;
- mandate/payment-method status.

Provider status never overwrites local rows generically. Each mismatch maps to a named repair rule.

## 16.3 Auto-repair boundaries

Safe auto-repairs:

- attach a provider ID to a uniquely matching pending operation;
- mark a payment succeeded when signed/provider-fetched evidence confirms exact amount/currency/invoice;
- ingest a missing event fact;
- refresh non-secret display metadata;
- rebuild stale projections from durable source.

Every auto-repair that writes a financial or subscription fact must use the same normalized evidence application service as webhook processing:

1. persist or lock a `platform_reconciliation_items` row;
2. normalize provider evidence through the adapter and compute the canonical `evidence_sha256`;
3. enter the same organization-scoped transaction and acquire the same aggregate locks used by webhook processing;
4. call the same idempotent domain transition function;
5. append `platform_subscription_events` with `source_type = 'reconciliation'`, `source_id = platform_reconciliation_items.id`, the canonical event type, and the shared `evidence_sha256`;
6. write outbox/projection updates in that transaction;
7. mark the reconciliation item `auto_repaired` only after commit.

Webhook and reconciliation races therefore serialize on the aggregate and deduplicate both by source identity and canonical evidence identity. The second path observes the already-applied fact and records a no-op/confirmed result rather than a second transition. Reconciliation code may not update payment, invoice, subscription, or period rows directly.

Manual review required:

- amount/currency mismatch;
- ambiguous tenant/customer mapping;
- duplicate provider customer/subscription;
- provider says canceled while local paid period appears valid;
- refund exceeds expected amount;
- invoice line/tax mismatch;
- payload identity collision;
- terminal fact contradiction.

## 16.4 Customer-triggered refresh

After returning from checkout, the UI may call a rate-limited refresh endpoint. The endpoint performs or queues a bounded provider lookup using known internal operation references. It never accepts arbitrary provider object IDs from the browser.

---

# 17. Security Specification

## 17.1 Threats that must have tests

```text
cross-tenant invoice read/download
cross-tenant plan-change command
forged organization ID/body field
forged checkout amount/price/currency
CSRF cancellation or plan change
stolen localStorage token
replayed webhook
forged webhook signature
valid signature with duplicate event
valid event mapped to wrong tenant
test webhook resolving a live mapping or live webhook resolving a test mapping when provider IDs collide
provider-object ID enumeration
support operator abuse
access override without approval/expiry
refund above captured amount
audit tampering
open redirect through checkout URL
raw provider secret/payload leakage
race consuming final entitlement slot
race scheduling two conflicting plan changes
remote success followed by local timeout
```

## 17.2 Authentication and step-up

Recent authentication is required for:

- changing payment method;
- upgrading/downgrading plan;
- cancellation;
- changing legal/tax identity;
- exporting high-volume data while restricted;
- internal refund, credit, override, and reconciliation repair.

Recommended recent-auth window: 10 minutes, policy-configurable.

MFA is required for internal control-plane actions and strongly recommended/required for organization owners before billing launch according to account-security rollout policy.

## 17.3 CSRF

For cookie-authenticated mutations:

- server issues CSRF token bound to session;
- frontend sends token in custom header;
- server validates Origin/Referer against allowlist where available;
- `GET`, `HEAD`, `OPTIONS` are side-effect free;
- provider webhook routes are excluded from CSRF but protected by provider signature.

## 17.4 Authorization and IDOR

- tenant comes from authenticated request state;
- billing object query always includes organization context or relies on forced RLS plus composite ownership;
- unauthorized cross-tenant object uses `404` where appropriate to avoid existence disclosure;
- document download uses a short-lived server-authorized stream/signed object URL created only after tenant and permission checks;
- provider IDs are never general API resource IDs.

## 17.5 Mass assignment

Pydantic request models use explicit fields and reject unknown fields. Fields such as the following never appear in customer mutation schemas:

```text
organization_id
status
amount_minor
tax_total_minor
provider_customer_id
provider_subscription_id
access_mode
entitlements
approved_by
```

## 17.6 Browser security

Required headers and policies:

- strict CSP compatible with approved hosted checkout;
- `frame-ancestors` deny except explicit need;
- HSTS in production;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy` minimizing leakage;
- secure cookie flags;
- no provider secret or sensitive billing state in URL query parameters;
- checkout return state uses signed, single-use, short-lived nonce.

## 17.7 Logging and telemetry

Never log:

- card/bank/UPI secrets;
- raw authorization/cookie headers;
- webhook signing secret;
- full raw provider payload;
- full tax registration;
- billing address unless operationally required and access-restricted;
- signed document URLs.

Use structured redaction and hash identifiers where exact value is unnecessary.

## 17.8 Internal control safeguards

- immutable operator identity;
- mandatory ticket/reference;
- reason codes plus free-text detail;
- four-eyes approval thresholds;
- no shared admin accounts;
- no direct production DB status updates as normal operations;
- emergency procedure is time-bounded, monitored, and retrospectively reviewed.

---

# 18. Frontend UX Specification

## 18.1 Information architecture

Sidebar labels:

```text
Member Subscriptions
Member Payments & Collections
Settings > Plan & Billing
```

Routes:

```text
/settings/plan-billing
/settings/plan-billing/invoices/:invoiceId
/settings/plan-billing/checkout-return
/billing-recovery
```

The old `/subscription-required` page becomes a compatibility redirect to `/billing-recovery`, preserving safe return location but preventing loops.

## 18.2 Canonical state provider

A single `PlatformAccessProvider` consumes `/platform-billing/summary` and exposes:

```text
access mode
reason
next transition
subscription summary
usage/limits
recovery actions
freshness/error state
```

It does not duplicate server decision logic.

React Query rules:

- summary cached briefly with server version;
- invalidate after billing commands;
- poll only while confirmation is pending, with capped exponential backoff;
- stop polling on terminal confirmation, user navigation, or timeout;
- show a manual “Check again” action after timeout;
- never optimistically mark payment or subscription active.

## 18.3 Account status banner

Only one top-level banner is shown. Priority:

```text
security/compliance block
billing-only
read-only
limited-write
payment action required
trial warning
scheduled cancellation/downgrade
informational
```

Banner requirements:

- plain language;
- exact date/time in user locale;
- one primary action and at most one secondary action;
- no repeated modal;
- accessible heading/status semantics;
- snooze/dismiss only when policy permits;
- dismissal does not suppress critical email/owner notification policy.

## 18.4 Disabled actions

Disabled controls include an accessible explanation and resolution action. Do not silently hide core controls unless visibility itself is unauthorized.

Example:

```text
Add member
Unavailable while your account is read-only. Update your payment method to restore changes.
```

## 18.5 Long forms and draft preservation

For forms that can take more than a trivial time:

- preflight capability on open;
- save non-sensitive draft locally or server-side;
- never persist payment details locally;
- if access changes before submit, retain values and present structured recovery;
- retry only after user action and a fresh idempotency key where semantics changed.

## 18.6 Plan selection

Plan cards display:

- total price and interval;
- tax behavior or clear estimate status;
- included limits/features;
- current usage impact;
- renewal date;
- annual/monthly comparison without manipulative dark patterns;
- cancellation/downgrade terms.

The confirmation screen shows a server-generated preview with expiry and hash/reference. Submission sends preview ID, not client-calculated totals.

## 18.7 Checkout return

States:

```text
confirming
requires_action
active
failed
confirmation_delayed
```

Copy examples:

- Confirming: “Payment submitted. We’re confirming it securely.”
- Delayed: “Confirmation is taking longer than usual. You can leave this page; we’ll update your account when confirmation arrives.”
- Active: “Your Doers plan is active.”
- Failed: provider-neutral safe message plus retry/update method.

A redirect alone never displays Active.

## 18.8 Cancellation

Cancellation flow:

1. show effective date and retained access;
2. show data/recovery behavior;
3. capture optional reason separately from authorization;
4. require recent authentication;
5. confirm in a focused dialog/page, not deceptive button design;
6. show scheduled cancellation persistently but calmly;
7. provide Undo until effective time.

## 18.9 Accessibility

Target WCAG 2.2 AA:

- full keyboard operation;
- logical focus after errors/dialogs/navigation;
- no color-only state;
- screen-reader status announcements for async confirmation;
- sufficient contrast;
- reduced-motion support;
- large touch targets;
- semantic tables for invoices and plan comparison;
- locale-aware dates/currency.

---

# 19. Notifications and Non-Irritation Controls

## 19.1 Dedupe identity

```text
organization_id + notification_type + lifecycle_source_id + policy_stage
```

The same notification is not sent repeatedly due to worker retries.

## 19.2 Channel policy

- in-app: canonical banner/notification centre;
- email: owner and configured billing contacts;
- SMS/WhatsApp: opt-in and policy/provider availability, except legally/contractually required notices where permitted;
- no marketing content mixed into critical billing notices.

## 19.3 Frequency limits

- no critical banner modal on every navigation;
- no more than one identical stage notification per channel;
- failure retry notices state next action/date;
- recovery confirmation sent once;
- snooze state stored server-side for cross-device consistency where appropriate.

## 19.4 Notification failure

Notification delivery failure never changes financial or access truth. It is retried and surfaced operationally. High-severity undelivered owner notices trigger support monitoring.

---

# 20. Observability and Service Objectives

## 20.1 Required metrics

```text
platform_billing_api_requests_total
platform_billing_api_latency_seconds
platform_billing_access_decisions_total{mode,reason}
platform_billing_access_projection_stale_total
platform_billing_entitlement_denials_total{feature}
platform_billing_provider_operations_total{type,status}
platform_billing_provider_unknown_outcomes_total
platform_billing_webhooks_received_total{provider,type}
platform_billing_webhook_processing_latency_seconds
platform_billing_webhook_dead_letters_total
platform_billing_reconciliation_mismatches_total{type,severity}
platform_billing_reconciliation_repairs_total
platform_billing_invoices_total{status}
platform_billing_payment_attempts_total{status}
platform_billing_dunning_stage_total{stage}
platform_billing_notification_failures_total{channel,type}
platform_billing_cross_tenant_denial_total
```

Do not use raw organization IDs as high-cardinality metric labels.

## 20.2 Initial service objectives

These are launch targets, measured separately from provider uptime:

- tenant billing summary availability: 99.9% monthly;
- durable valid-webhook acceptance: 99.95% monthly when database is available;
- p95 summary latency: under 400 ms from primary DB path under expected load;
- p95 webhook durable acknowledgement: under 1 second excluding provider/network transit;
- 99% of accepted webhooks processed within 2 minutes under normal operation;
- critical reconciliation mismatch acknowledged operationally within 15 minutes;
- zero known cross-tenant disclosure;
- zero duplicate financial effect from repeated command/event.

## 20.3 Alerts

Page/on-call:

- webhook dead-letter burst;
- unknown provider outcomes beyond threshold;
- access projection ahead of source;
- critical reconciliation mismatch;
- duplicate provider identity collision;
- cross-tenant test/probe failure;
- invoice arithmetic invariant failure;
- database/RLS context failure.

Ticket/working-hours alert:

- notification backlog;
- stale usage snapshots;
- elevated provider latency;
- non-critical reconciliation drift.

---

# 21. Failure Behavior

## 21.1 Redis unavailable

- authorization and billing reads fall back to PostgreSQL;
- rate limiting uses defined local/degraded behavior without granting unauthorized access;
- no subscription state changes solely because cache is unavailable;
- cache write failures are logged/metricized, not exposed as customer payment failures.

## 21.2 Celery unavailable

- accepted webhooks remain in durable inbox;
- lifecycle truth remains derivable by timestamps;
- user-triggered bounded refresh can reconcile pending checkout;
- no automatic lockout caused only by scheduler delay;
- backlog alerts fire.

## 21.3 Provider unavailable

- existing valid paid/trial periods continue;
- checkout/payment updates show temporary unavailability;
- provider operations remain reserved/retryable/unknown as appropriate;
- no guessed success or failure;
- dunning transitions require durable timing/evidence, not transient API error.

## 21.4 Database unavailable

- no financial mutation proceeds;
- webhook endpoint cannot acknowledge durable acceptance and must return provider-retry-compatible failure;
- frontend shows service unavailable and preserves non-sensitive drafts;
- no fallback datastore grants access.

## 21.5 Notification provider unavailable

- lifecycle proceeds according to durable policy;
- delivery records retry;
- operational alert if critical notices remain undelivered;
- in-app status remains available.

## 21.6 Projection worker unavailable

- source version mismatch triggers the synchronous resolver with the frozen 150 ms total budget;
- safe reads may use only the §8.4 fallback mapping after independent tenant/resource authorization and durable security-block checking;
- fallback never guesses `full` and never guesses `blocked`; it yields only `read_only` or `billing_only`;
- exports, financial actions, admin operations, capacity changes, destructive actions, and privileged writes never use fallback;
- privileged writes fail closed with `503 ACCESS_DECISION_UNAVAILABLE` if fresh resolution cannot be obtained;
- projection lag and fallback-rate alerts fire.

---

# 22. Migration from Current Trial/Tier System

## 22.1 Current elements to retire gradually

```text
TrialSubscription
TrialService as access authority
require_trial_active
Organization.tier as authorization source
Organization.max_branches as platform plan authority
TIER_LIMITS
frontend TrialLockBanner
HARD_LOCKED/SOFT_LOCKED global redirect behavior
```

They remain temporarily for comparison and rollback only.

## 22.2 Backfill rules

For every organization:

1. create/confirm billing account draft from organization data; do not mark tax identity verified automatically;
2. map existing trial timestamps into a new trial subscription and periods;
3. preserve original timestamps and status in migration metadata;
4. map current tier to an internal migration plan version only through an explicit mapping table;
5. compute V3 access projection;
6. record migration event with source row ID/hash;
7. do not delete old trial row.

Ambiguous/invalid rows enter migration review, not guessed state.

## 22.3 Shadow mode

For a defined period:

- old trial logic remains enforcement authority;
- V3 resolver computes decisions for every relevant request/event;
- compare old and new results;
- record reasoned differences;
- expected policy differences are allowlisted with expiry;
- unexplained differences block cutover.

## 22.4 Cutover

1. freeze trial/tier mutations briefly or dual-write through one compatibility service;
2. ensure backfill high-watermark complete;
3. verify projection freshness and RLS tests;
4. enable V3 enforcement for internal tenants;
5. pilot tenants;
6. progressive percentage/tenant allowlist;
7. disable old frontend redirects;
8. remove old enforcement dependencies only after rollback window;
9. retain historical tables until approved archival migration.

## 22.5 Rollback

Feature flags separately control:

```text
platform_billing_read_api
platform_billing_shadow_resolver
platform_billing_enforcement
platform_billing_frontend_shell
platform_billing_checkout
platform_billing_webhook_processing
platform_billing_dunning_transitions
platform_billing_notifications
```

Disabling checkout or dunning must not disable invoice viewing or recovery access.

---

# 23. Test Specification

## 23.1 Domain unit tests

- every allowed/forbidden state transition;
- access resolver boundary at exact timestamps;
- policy-day duration equals exactly 86,400 elapsed seconds and is not calendar-day aligned in UTC or organization timezone;
- monthly/yearly contractual intervals use persisted calendar boundaries rather than 30/365-day substitution;
- trial, dunning, cancellation, post-cancel windows;
- override priority and expiry;
- entitlement composition and default deny;
- money arithmetic and rounding;
- invoice arithmetic;
- refund cumulative ceiling;
- capability/access matrix.

Use property-based tests for time boundaries, money invariants, and transition sequences where practical.

## 23.2 Database tests

- composite FK rejects cross-tenant child;
- forced RLS hides/rejects cross-tenant rows;
- app role cannot bypass RLS;
- financial rows resist delete/update;
- published catalogue immutability;
- one-current-subscription partial unique constraint;
- two concurrent first-subscription commands serialize on the organization advisory lock, produce one current contract, and return a deterministic replay/conflict for the loser;
- unique index still rejects duplicate current contracts when the application lock is deliberately bypassed in a database test;
- period overlap exclusion;
- one default payment method;
- concurrent final-slot capacity creation;
- concurrent plan-change conflict;
- sequence allocation;
- migration upgrade/downgrade on production-like schema snapshot.

## 23.3 API tests

- organization ID body injection rejected;
- every route declares capability;
- structured restriction responses;
- each §11.2 route enforces its exact `If-Match` rule;
- missing required ETag returns 428 and stale/mismatched ETag returns 412;
- initial checkout may omit `If-Match` only when the server confirms no current subscription exists;
- missing/changed idempotency key behavior;
- CSRF protection;
- recent-auth requirement;
- invoice document tenant isolation;
- billing recovery endpoints remain available in restricted modes;
- no redirect-authorized activation.

## 23.4 Webhook tests

- invalid signature;
- oversized body;
- duplicate same payload;
- duplicate ID/different payload anomaly;
- delayed event;
- out-of-order event;
- processing crash before/after domain commit;
- lease expiry;
- dead-letter/replay;
- unmapped provider object;
- cross-tenant mapping attempt;
- identical provider object IDs in test/live mappings, proving a test webhook cannot resolve live and a live webhook cannot resolve test;
- environment value in payload cannot override endpoint/signing-secret environment context;
- provider current-object fetch before corrective transition.

## 23.5 Provider saga tests

- remote failure before creation;
- remote success and local timeout;
- remote timeout with unknown outcome;
- duplicate local request;
- retry with same provider idempotency key;
- reconciliation discovers success;
- webhook and reconciliation concurrently observe the same payment-success fact and produce exactly one domain transition/outbox effect using shared `evidence_sha256`;
- replay of the same reconciliation item is a no-op;
- reconciliation cannot mutate financial rows outside the normalized evidence application service;
- no duplicate provider subscription/charge/refund.

## 23.6 Frontend tests

Add and pin:

- Vitest;
- React Testing Library;
- MSW;
- Playwright for critical flows.

Test:

- one canonical banner;
- no redirect loop;
- draft preservation;
- keyboard/focus behavior;
- payment confirming vs active;
- delayed confirmation;
- plan preview expiry;
- downgrade over-limit explanation;
- cancellation and undo;
- restricted capability explanation;
- invoice accessibility;
- local storage contains no auth token after migration.

## 23.7 Security tests

- automated tenant A/B matrix for every billing resource;
- CSRF and origin validation;
- replay and tamper tests;
- open redirect rejection;
- mass-assignment fuzzing;
- provider payload/log redaction;
- secret scanning;
- dependency/container scans;
- SAST;
- manual threat-model review before provider launch.

## 23.8 Reliability/load tests

- webhook burst with duplicates;
- worker restart mid-batch;
- Redis outage;
- Celery outage/backlog recovery;
- provider latency/timeout storm;
- DB failover/retry behavior;
- reconciliation of missing events;
- synchronous access resolution completes within/over the frozen 150 ms budget and returns the specified result/503;
- stale safe-read fallback mapping for every prior access mode, including no guessed `full` or `blocked`;
- concurrent entitlement consumption;
- invoice generation retry;
- backup restore and replay from high-watermark.

---

# 24. Verification Commands

Commands are run from the corresponding repository root. Adjust only when the repository’s canonical tooling changes and document the change.

## Backend baseline

```bash
python -m compileall app
pytest -q
alembic heads
alembic current
```

Before a migration is accepted:

```bash
alembic upgrade head
pytest -q tests/platform_billing
alembic downgrade -1
alembic upgrade head
pytest -q tests/platform_billing
```

Run upgrade/downgrade against a disposable PostgreSQL database that includes required extensions and roles. SQLite is not sufficient for RLS, exclusion constraints, advisory locks, or PostgreSQL triggers.

## Frontend baseline

```bash
npm ci
npm run lint
npm run build
npm run test
npm run test:e2e
```

The agent may add `test` and `test:e2e` scripts in the authorized frontend testing phase.

## Repository hygiene

```bash
git status --short
git diff --check
git diff --stat
```

No phase is complete with unreported generated files, secrets, broad formatting churn, or unrelated changes.

---

# 25. Phase-by-Phase Implementation Plan

## Phase 0 — Baseline and Constitutional Guardrails

**Goal:** Make implementation safe before creating billing tables.

Deliverables:

- confirm one intended Alembic head or document/resolve multiple-head strategy;
- add V2 and the consolidated V3.1 document under repository architecture docs;
- add package skeleton;
- add policy YAML schemas/loaders with no production commercial values;
- add and validate `platform_billing_runtime_v1.yaml` with the frozen §1.7 defaults;
- add architecture tests proving services import the centralized runtime defaults rather than redefining them;
- add domain enums/errors/money types;
- add architecture tests preventing imports between Platform Billing and facility commerce;
- create feature-flag definitions;
- document session migration plan;
- no behavior change.

Authorized areas:

```text
app/platform_billing/**
tests/platform_billing/**
docs/architecture/**
app/core/config.py (feature flags only)
```

Verification:

```text
existing backend suite
policy schema tests
runtime-default singleton/constant-drift tests
forbidden-import test
no migration generated
```

Suggested commit:

```text
chore(platform-billing): add constitutional execution foundation
```

Stop after report. Do not create database tables in Phase 0.

## Phase 1 — Additive Database and Read Model Foundation

**Goal:** Create catalogue, billing account, subscription, period, event, and audit foundation without enforcing access.

Tables:

```text
platform_products
platform_policy_versions
platform_plan_versions
platform_prices
platform_feature_definitions
platform_plan_entitlements
platform_billing_accounts
platform_subscriptions
platform_subscription_items
platform_subscription_periods
platform_subscription_events
platform_billing_audit_events
```

Deliverables:

- hand-authored Alembic migration;
- SQLAlchemy mappings;
- tenant composite constraints;
- FORCE RLS;
- immutable triggers;
- internal migration-only plan/policy seed;
- read-only repository/query service;
- no live provider;
- no route enforcement;
- migration tests.

Critical rule: do not auto-generate and blindly accept Alembic output.

Suggested commit:

```text
feat(platform-billing): add subscription database foundation
```

## Phase 2 — Resolver, Entitlements, and Shadow Projection

**Goal:** Implement deterministic access and entitlement calculation without changing customer authorization.

Tables:

```text
platform_subscription_changes
platform_access_overrides
platform_entitlement_projection
platform_access_projection
platform_usage_projection
```

Deliverables:

- state machine;
- access resolver;
- entitlement resolver;
- projection writer;
- outbox integration;
- backfill service for current trials;
- shadow comparison with old trial logic;
- diagnostics endpoint restricted to internal/dev use;
- no enforcement cutover.

Suggested commit:

```text
feat(platform-billing): add lifecycle and entitlement projections
```

## Phase 3 — Capability Enforcement and Frontend Clarity

**Goal:** Establish universal server-side enforcement and a non-irritating UI shell, initially feature-flagged/shadowed.

Backend:

- capability registry;
- route manifest/startup validator;
- capability guard;
- structured errors;
- freshness logic;
- capacity guard API;
- migration adapters replacing direct tier reads one bounded domain at a time.

Frontend:

- rename member-commerce navigation labels;
- add Plan & Billing route and read-only summary;
- replace `TrialLockBanner` with canonical account status banner;
- remove unconditional `HARD_LOCKED` redirect behavior;
- add billing recovery page;
- preserve form drafts;
- add frontend test harness.

Enforcement rollout remains feature-flagged by tenant.

Suggested commits may be split backend/frontend:

```text
feat(platform-access): add capability-based subscription enforcement
feat(platform-billing-ui): add plan and billing recovery experience
```

## Phase 4 — Provider-Neutral Checkout and Webhook Inbox

**Goal:** Integrate the fake provider and durable provider infrastructure before a real provider.

Tables:

```text
platform_provider_customers
platform_payment_methods
platform_provider_operations
platform_webhook_inbox
platform_reconciliation_runs
platform_reconciliation_items
```

Deliverables:

- provider interface/registry;
- deterministic fake provider;
- checkout and payment-method setup commands;
- provider saga;
- signed fake webhook harness;
- durable webhook acceptance/worker;
- reconciliation engine;
- confirming/delayed checkout-return UX;
- production checkout remains disabled.

Suggested commit:

```text
feat(platform-billing): add provider-neutral checkout reliability layer
```

## Phase 5 — Real Provider Adapter in Sandbox

**Goal:** Add one approved India-capable provider adapter behind the same contract.

Prerequisites:

- provider release manifest approved for sandbox;
- threat model updated;
- secrets management ready;
- webhook endpoint registered;
- provider contract tests written.

Deliverables:

- adapter only; no provider-specific branches in domain services;
- hosted checkout;
- recurring mandate/payment method support as available;
- signature verification;
- normalized events;
- sandbox reconciliation;
- operational runbook;
- kill switch.

No live-money activation.

Suggested commit:

```text
feat(platform-billing): add sandbox provider adapter
```

## Phase 6 — Financial Ledger, Invoicing, Refunds, and Credits

**Goal:** Introduce immutable financial records and accounting workflows.

Tables:

```text
platform_document_sequences
platform_invoices
platform_invoice_lines
platform_payment_attempts
platform_refunds
platform_credit_notes
platform_credit_note_lines
platform_mandates
```

Deliverables:

- invoice arithmetic and issue transaction;
- billing/seller/tax snapshots;
- immutable PDF/document hash;
- payment allocation;
- refund cumulative checks;
- credit note workflow;
- finance/legal review of India invoice/tax behavior;
- invoice UI/download;
- no live activation until acceptance gates.

Suggested commit:

```text
feat(platform-billing): add immutable platform financial ledger
```

## Phase 7 — Dunning, Notifications, and Lifecycle Automation

**Goal:** Automate graceful lifecycle changes without surprise lockouts.

Deliverables:

- lifecycle tick using DB time;
- policy-driven dunning stages;
- notification delivery/dedupe;
- owner/billing-contact notice schedule;
- recovery confirmation;
- provider/scheduler outage behavior;
- no transition based only on failed notification.

Suggested commit:

```text
feat(platform-billing): add policy-driven dunning and recovery
```

## Phase 8 — Session Security and Internal Control Plane

**Goal:** Complete the privileged security model before production billing.

Deliverables:

- cookie-only browser auth target;
- remove auth tokens from local storage;
- CSRF protection;
- recent-auth and MFA gates;
- internal billing permissions;
- four-eyes refund/override approval;
- audit review tools;
- secret rotation runbook;
- penetration/security testing.

Suggested commit:

```text
security(platform-billing): harden sessions and operator controls
```

## Phase 9 — Migration, Pilot, and Production Rollout

**Goal:** Move from old trial/tier authority to V3 safely.

Deliverables:

- production backfill dry run;
- shadow-decision report with zero unexplained differences;
- internal tenant rollout;
- pilot tenants;
- progressively enabled enforcement;
- live provider feature flag;
- monitored first invoice/renewal/refund;
- rollback drill;
- old trial enforcement retired only after success window.

Suggested commit:

```text
feat(platform-billing): complete controlled subscription cutover
```

---

# 26. Agent-Safe Rules

Every coding agent must follow these rules:

1. Do not implement more than one phase unless explicitly instructed.
2. Do not rename or reuse facility-commerce tables for Platform Billing.
3. Do not calculate authoritative price/tax in the browser.
4. Do not activate access from checkout redirect.
5. Do not use Redis as the only lock, idempotency, or authorization boundary.
6. Do not perform provider calls inside a long database transaction.
7. Do not introduce a route without capability classification.
8. Do not accept tenant identity from ordinary client input.
9. Do not add cascade deletion to financial/history tables.
10. Do not create non-expiring access overrides.
11. Do not log raw payment data, provider secrets, or full webhook payloads.
12. Do not change published catalogue or issued invoice business fields.
13. Do not silently repair ambiguous reconciliation mismatches.
14. Do not auto-deactivate customer data on downgrade or expiry.
15. Do not commit unless the user/reviewer asks.
16. Report all pre-existing failures separately from introduced failures.
17. Stop when a constitutional invariant cannot be satisfied; do not improvise a weaker design.

---

# 27. Launch Acceptance Gates

Production billing remains disabled until every gate is evidenced.

## 27.1 Domain separation

- no Platform Billing dependency on facility-commerce models/services;
- distinct routes, permissions, tables, UI labels, and reports;
- automated forbidden-import test passes.

## 27.2 Tenant isolation

- forced RLS verified using real PostgreSQL roles;
- cross-tenant matrix passes for every object/document endpoint;
- background worker mapping cannot ambiguously select tenant;
- internal APIs separately authenticated and audited.

## 27.3 Financial correctness

- money arithmetic/property tests pass;
- issued document immutability passes;
- duplicate command/webhook creates one financial effect;
- refund limit enforced under concurrency;
- reconciliation detects injected mismatch;
- finance/legal approve invoice/tax behavior.

## 27.4 Reliability

- remote success/local timeout test passes;
- duplicate/delayed/out-of-order/missing webhook tests pass;
- Redis/Celery/provider outage drills pass;
- dead-letter replay works;
- backup restore plus reconciliation drill passes;
- kill switches independently verified.

## 27.5 Security

- cookie/CSRF/recent-auth model active;
- no auth token in browser storage;
- secret scanning clean;
- threat model reviewed;
- security tests/penetration findings resolved or formally accepted;
- operator four-eyes controls verified.

## 27.6 UX

- no surprise lockout;
- no redirect loop;
- draft preservation verified;
- checkout confirmation language accurate;
- cancellation and downgrade transparent/reversible as policy allows;
- WCAG 2.2 AA review of critical flows;
- user testing confirms billing/member-commerce terminology is not confused.

## 27.7 Operations

- dashboards and alerts active;
- provider, webhook, reconciliation, refund, data restoration, and incident runbooks rehearsed;
- on-call ownership assigned;
- support can diagnose without direct DB mutation;
- first-renewal monitoring plan approved.

---

# 28. Required Runbooks

Before launch, publish and rehearse:

```text
provider outage
webhook backlog/dead letter
unknown provider operation outcome
customer paid but access not active
duplicate charge allegation
refund request/failure
chargeback/dispute
invoice correction/credit note
wrong tax identity
ambiguous provider customer mapping
access override request/revocation
RLS/tenant-isolation incident
billing data restore and reconciliation
secret rotation
provider migration
feature-flag rollback
```

Each runbook contains owner, severity, detection, immediate containment, safe customer communication, exact commands/tools, escalation, evidence retention, and post-incident review.

---

# 29. Explicit Deferred Capabilities

These are designed for future compatibility but are not required for first production release unless commercial policy selects them:

- usage-based billing;
- multiple simultaneous base subscriptions per organization;
- multiple legal billing accounts per tenant;
- multi-currency contract conversion;
- marketplace revenue sharing;
- reseller billing;
- complex coupon engine;
- custom enterprise contract amendments;
- cross-provider active-active billing;
- automatic organization merge;
- automatic tax determination across all jurisdictions.

Deferred features must not be partially implemented through ad hoc columns or provider-specific shortcuts.

---

# 30. Definition of Done for V3

V3 is considered implemented only when:

1. all accepted phases have their required database/application/frontend artifacts;
2. every route is capability-classified;
3. every current organization has a deterministically resolvable platform-access state;
4. provider retries and missing events cannot duplicate or lose financial effect;
5. customer-facing states remain clear and recoverable;
6. tenant isolation is demonstrated, not assumed;
7. production commercial and provider manifests are approved;
8. launch gates and runbooks are complete;
9. old trial/tier authorization is retired through controlled migration;
10. V2 invariants remain true under normal operation, concurrency, outage, retry, rollback, and support intervention.

---

# Final Execution Statement

Doers will not treat subscription billing as a payment button attached to a trial flag. It will operate Platform Billing as a separate commercial and authorization institution with durable contracts, immutable financial evidence, deterministic entitlements, capability-based access, provider reconciliation, strict tenant isolation, controlled internal intervention, and a calm customer recovery experience.

The authorized next implementation action after approval of this document is **Phase 0 only**. Phase 0 creates the guardrails and package foundation; it does not create billing tables or connect a payment provider.