# Doers Platform Subscription Constitution — V2

**Status:** Authoritative architecture and implementation constitution  
**Date:** 15 June 2026  
**Scope:** Facilities and organizations subscribing to the Doers SaaS platform  
**Explicitly out of scope:** Membership plans, subscriptions, payments, and invoices that a facility manages for its own members  
**Implementation status:** Architecture only; no application code is authorized by this document alone

---

## 0. Executive Decision

Doers will implement platform subscriptions as a separate, security-critical bounded context named **Platform Billing**.

A gym, yoga studio, calisthenics centre, martial-arts academy, dance studio, wellness centre, or similar facility is a **Doers customer organization**. Its commercial contract with Doers is entirely separate from the membership contracts it sells to its own members.

The platform billing system must satisfy five product promises:

1. **Reliable:** provider delays, duplicate webhooks, Redis outages, scheduler failures, retries, and user double-clicks must not corrupt billing state.
2. **Secure:** tenant isolation, payment-data minimization, step-up authentication, immutable audit, and least privilege are mandatory.
3. **Non-irritating:** warnings are calm, clear, predictable, and recoverable. No repeated modals, surprise lockouts, redirect loops, or lost work.
4. **Transparent:** prices, tax, renewal date, limits, downgrade impact, cancellation timing, and payment state are shown before confirmation.
5. **Reversible where safe:** users can cancel a scheduled downgrade or cancellation before it takes effect; support actions are controlled, expiring, and audited.

Security is not traded for convenience. Convenience is created by designing secure flows that users can understand.

---

# Part I — Constitutional Boundaries

## 1. Two Financial Domains That Must Never Be Mixed

### 1.1 Platform Billing

An organization pays Doers for access to the SaaS platform.

Examples:

- Doers Starter monthly plan
- Doers Growth annual plan
- additional branch add-on
- advanced reporting entitlement
- trial, renewal, failed payment, cancellation, credit note, refund

Recommended namespace:

```text
Backend package:       app/platform_billing/
Database table prefix: platform_
API prefix:            /organizations/{org_id}/platform-billing
Frontend feature:      src/features/platformBilling/
Frontend route:        /settings/plan-billing
```

### 1.2 Facility Commerce

A facility creates plans and collects payments from its members.

Existing concepts such as the following belong here:

```text
membership_plans
member_subscriptions_v2
payments
invoices
/subscriptions
/billing
```

These tables, services, screens, invoice numbers, payment attempts, and reports must never be used for the organization’s payment to Doers.

### 1.3 Hard Invariant

A member subscription can never grant, revoke, renew, upgrade, downgrade, suspend, or otherwise influence an organization’s access to Doers.

This invariant must be enforced by package boundaries, database names, API paths, permissions, tests, and user-facing terminology.

---

## 2. Current Repository Assessment

The uploaded repositories contain useful infrastructure but only a prototype for SaaS access.

### 2.1 Foundations to retain

- FastAPI and PostgreSQL transactional architecture
- tenant context and existing RLS patterns
- composite tenant foreign-key patterns
- DB-backed idempotency engine
- transactional outbox foundations
- advisory-lock utilities
- Redis and Celery infrastructure
- audit infrastructure
- organization, branch, owner, and staff identities
- frontend API client, route guards, shared layout, and design tokens

### 2.2 Existing elements that must not be extended into Platform Billing

- `app/models/subscription.py`
- `app/models/payment.py`
- `app/services/subscription_service.py`
- `app/services/payment_service.py`
- `member_subscriptions_v2`
- frontend `/subscriptions`
- frontend `/billing`

They represent facility-to-member commerce.

### 2.3 Current SaaS-access weaknesses that V2 replaces

1. `Organization.tier`, `Organization.max_branches`, `TIER_LIMITS`, and database limit logic create multiple sources of truth.
2. `TrialSubscription` is a mutable single-row prototype with string status values.
3. `TrialSubscription.plan_id` is not a protected catalog foreign key.
4. Trial records cascade-delete with the organization, which is unsuitable for financial and commercial history.
5. `require_trial_active` exists but is not attached universally to protected routes.
6. Its current policy treats HTTP methods as permissions: GET is assumed safe and writes are blocked. Platform access must be capability-based instead.
7. Trial locking is surfaced by frontend interception, but frontend behavior is not an authorization boundary.
8. `/subscription-required` directs users toward member subscriptions rather than Doers plan billing.
9. The frontend persists bearer tokens in local storage while the backend also supports HttpOnly cookies. Privileged billing flows require one formally selected web-session model; dual token paths increase complexity and attack surface.
10. Current trial text such as “Membership Protocol” is visually distinctive but too indirect for critical payment and account-access communication.

### 2.4 Migration rule

The new system is introduced additively. Existing member-commerce code is not renamed at the database level during the first billing phase. User-facing labels and routes are clarified first; data migrations are deliberate and reversible.

---

## 3. Architectural Invariants

These rules cannot be overridden by ordinary implementation decisions:

1. Platform billing and member commerce never share commercial records.
2. The payment provider is evidence and execution infrastructure; it is not Doers’ authorization engine.
3. Doers access is derived locally from durable contract, period, dunning, override, and entitlement records.
4. Published plan versions and prices are immutable.
5. Issued invoices and completed financial events are immutable; corrections use explicit reversal, void, credit, or refund records.
6. Every tenant-owned billing row carries `organization_id` and is protected by tenant-scoped constraints.
7. Every financially meaningful mutation is idempotent.
8. Webhooks are assumed to be duplicated, delayed, retried, missing, malformed, and out of order.
9. The browser never supplies authoritative amount, tax, entitlement, invoice state, access state, or provider object identity.
10. Schedulers improve timeliness but never define truth. Effective state must remain derivable from durable timestamps and records.
11. A downgrade or expiry never silently deletes customer data.
12. Monetary values use integer minor units and explicit ISO currency.
13. Canonical timestamps use UTC. Locale conversion is display-only.
14. Redis is never the sole correctness or authorization boundary.
15. Celery is never the sole lifecycle authority.
16. User-facing recovery remains available even when normal product access is restricted.
17. Internal operator actions are more strictly controlled than tenant actions, not less.
18. No production launch occurs without tenant-isolation, replay, concurrency, restore, and provider-outage tests.

---

# Part II — Product and UX Constitution

## 4. Non-Irritating UX Charter

### 4.1 The UI must not punish the user for distributed-system behavior

Payment processing can take time. Webhooks can be delayed. Bank mandates can require action. The UI must distinguish:

- payment initiated;
- awaiting customer action;
- processing;
- confirmed;
- failed;
- confirmation delayed;
- provider unavailable.

A successful redirect from a payment provider must display **“Payment submitted—confirming”**, not **“Subscription activated”**, until Doers has verified durable provider state.

### 4.2 One canonical account-status surface

Doers will use one canonical platform-billing status model throughout the application. The same source powers:

- dashboard banner;
- Plan & Billing page;
- disabled-action explanations;
- billing recovery shell;
- support diagnostics.

Multiple components must not invent their own interpretation of trial, past due, or locked state.

### 4.3 Notification hierarchy

#### Informational

Examples: trial has 10 days remaining, annual invoice is ready.

- inline card or notification centre;
- dismissible;
- no modal;
- does not interrupt work.

#### Attention required

Examples: trial ends in 3 days, renewal payment needs action.

- persistent but compact top banner;
- one clear primary action;
- dismissible for a defined snooze period when safe;
- does not reappear on every navigation within the snooze window.

#### Restricted

Examples: read-only stage or billing-only stage.

- persistent application-shell explanation;
- controls are disabled with an accessible reason;
- navigation remains available to permitted pages;
- drafts and entered data are preserved;
- no redirect loop.

#### Security/compliance block

- dedicated blocking screen;
- plain reason category without exposing sensitive fraud signals;
- support/recovery path where legally and operationally permitted;
- no misleading payment prompt when payment cannot resolve the block.

### 4.4 No surprise lockouts

Recommended default communication schedule, implemented as versioned policy rather than hard-coded values:

- trial start confirmation;
- 7 days remaining;
- 3 days remaining;
- 1 day remaining;
- trial ended / grace started;
- access-mode transition warning;
- each payment failure with next retry date;
- read-only warning before it becomes effective;
- cancellation and downgrade reminders before effective date.

Users can configure non-critical channels, but owner-level financial notices cannot be fully disabled.

### 4.5 No lost work

Before a restricted write:

1. frontend preflights current access when opening a long form;
2. form drafts are preserved locally or server-side where appropriate;
3. backend returns a structured restriction response if state changed during editing;
4. UI explains what happened and preserves entered values;
5. user can navigate directly to resolution.

### 4.6 Plain language

Critical billing copy must say exactly what happened:

Good:

```text
Your trial ended on 20 June. You can still view and export your data until 27 June.
Choose a plan to keep creating members and branches.
```

Avoid:

```text
Membership protocol concluded.
Account hard-locked.
Lifecycle exception encountered.
```

Internal state names may remain technical; customer copy must not be.

### 4.7 Destructive-action UX

Cancellation, immediate plan change, tax-identity change, refund, and billing-admin removal require:

- explicit impact summary;
- effective date;
- amount or credit impact;
- resources/features affected;
- confirmation text;
- recent authentication when risk warrants it;
- durable receipt after completion.

No dark patterns, preselected upgrades, hidden renewal terms, or confusing cancellation routes.

### 4.8 Accessibility

Plan and billing journeys target WCAG 2.2 AA:

- full keyboard navigation;
- visible focus;
- semantic headings and tables;
- accessible error summaries;
- no color-only status;
- adequate target size;
- screen-reader announcements for processing and completion;
- reduced-motion support;
- accessible charts with textual equivalents.

---

## 5. Frontend Information Architecture

### 5.1 Existing facility-commerce labels

```text
/subscriptions  -> label “Member Subscriptions”
/billing        -> move toward /collections and label “Member Payments & Collections”
```

### 5.2 Platform Billing routes

```text
/settings/plan-billing
/settings/plan-billing/plans
/settings/plan-billing/invoices
/settings/plan-billing/payment-method
/settings/plan-billing/tax-profile
/settings/plan-billing/billing-users
/settings/plan-billing/history
```

A dedicated recovery shell can use:

```text
/account-recovery/billing
```

The old `/subscription-required` route should become a compatibility redirect to the recovery shell, never to member subscriptions.

### 5.3 Plan & Billing page structure

1. Account status and next important date
2. Current plan, billing interval, and price
3. Usage versus limits
4. Available actions from backend
5. Payment method / mandate status
6. Upcoming invoice or renewal estimate
7. Invoice history
8. Billing and tax identity
9. Subscription history
10. Support and export

### 5.4 Required components

```text
PlatformBillingStatusBanner
CurrentPlanCard
EntitlementUsageMeter
PlanComparisonGrid
PlanChangeImpactPanel
PaymentConfirmationPanel
PaymentMethodCard
MandateStatusCard
UpcomingInvoiceCard
InvoiceTable
TaxProfileForm
BillingContactManager
CancellationPanel
BillingRecoveryLayout
```

### 5.5 Frontend authority rule

The frontend may hide unavailable actions for usability, but it never decides authorization. The backend returns an `available_actions` collection and validates every submitted operation again.

### 5.6 Financial optimistic updates

Do not optimistically mark the following as complete:

- subscription activation;
- upgrade/downgrade;
- cancellation;
- payment-method replacement;
- refund;
- invoice payment;
- mandate activation.

Use explicit processing states and refresh from the server.

---

# Part III — Commercial and Domain Model

## 6. Catalog and Versioning

### 6.1 Tables

```text
platform_products
platform_plan_versions
platform_prices
platform_features
platform_plan_entitlements
platform_addons
platform_addon_prices
platform_trial_policies
platform_dunning_policies
```

### 6.2 Product

Represents the general Doers platform product or future product family.

Key fields:

```text
id
code
name
description
status
created_at
```

### 6.3 Plan version

A plan version is the immutable commercial and entitlement definition.

Key fields:

```text
id
product_id
plan_code
version_number
display_name
description
status: draft | published | retired
published_at
available_from
available_until
metadata_json
created_by
created_at
```

Constraints:

- unique `(product_id, plan_code, version_number)`;
- published records cannot be edited except operational retirement fields;
- a new price or limit creates a new version;
- existing subscribers retain their contracted version unless migrated explicitly.

### 6.4 Price

```text
id
plan_version_id
country_code
currency
amount_minor
billing_interval: month | year | one_time
interval_count
tax_behavior: inclusive | exclusive | unspecified
provider_price_mapping
valid_from
valid_until
status
```

Rules:

- currency is never inferred from browser locale;
- no live FX conversion in the initial India-first system;
- price selection is performed server-side from catalog and organization billing country;
- provider price IDs are mappings, not core identity.

### 6.5 Recommended initial packaging principle

Plans should be based primarily on business scale and capabilities, not facility label. A yoga studio and a calisthenics centre with similar branch/member scale should not require separate billing architecture.

Facility-type-specific templates may exist as onboarding or feature presets, while entitlements remain composable.

---

## 7. Billing Account and Legal Identity

### 7.1 Tables

```text
platform_billing_accounts
platform_billing_contacts
platform_tax_profiles
platform_provider_customers
platform_billing_addresses
```

### 7.2 One initial billing account per organization

The first release supports one billing account for each Doers organization. The schema must not prevent future parent-group or consolidated billing.

### 7.3 Billing account fields

```text
id
organization_id
legal_name
display_name
billing_email
billing_phone
country_code
default_currency
locale
timezone
invoice_delivery_preferences
status
created_at
updated_at
```

### 7.4 Tax profile

Store only data necessary for invoicing and compliance. Sensitive identifiers should be masked in normal reads and encrypted where appropriate.

Changes to legal name, tax identifier, or billing country:

- require billing permission;
- require recent authentication;
- create an audit event;
- affect future invoice snapshots only;
- never rewrite issued invoices.

---

## 8. Subscription Contract Model

### 8.1 Tables

```text
platform_subscriptions
platform_subscription_items
platform_subscription_periods
platform_subscription_changes
platform_subscription_events
platform_dunning_cases
platform_dunning_attempts
```

### 8.2 Subscription

The long-lived commercial contract.

Key fields:

```text
id
organization_id
billing_account_id
provider_type
contract_status
current_period_id
cancel_at_period_end
scheduled_change_id
version
created_at
updated_at
```

### 8.3 Subscription item

Supports base plan and future add-ons.

```text
id
subscription_id
item_type: base_plan | addon
plan_version_id or addon_id
quantity
price_id
effective_from
effective_until
```

### 8.4 Subscription period

Preserves each trial or paid service interval.

```text
id
subscription_id
period_type: trial | paid | complimentary | manual
starts_at
ends_at
access_grace_ends_at
billing_anchor
source_invoice_id
status
created_at
```

Periods are append-oriented. Conversion from trial to paid creates a new period; it does not rewrite the trial.

### 8.5 Subscription change

Represents requested intent and its lifecycle.

```text
id
organization_id
subscription_id
change_type: upgrade | downgrade | cancel | reactivate | pause | resume
requested_by
requested_at
effective_at
from_snapshot
to_snapshot
price_impact_snapshot
status: requested | awaiting_payment | scheduled | applied | canceled | failed
idempotency_key
failure_code
```

### 8.6 Subscription event

Append-only domain history:

```text
id
organization_id
subscription_id
event_type
occurred_at
effective_at
actor_type
actor_id
causation_id
correlation_id
provider_event_reference
safe_metadata_json
```

No secrets or unredacted payment data belong in event metadata.

---

## 9. Independent State Dimensions

One overloaded `status` field is prohibited.

### 9.1 Contract status

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

### 9.2 Invoice status

```text
draft
open
paid
void
uncollectible
```

### 9.3 Payment attempt status

```text
created
requires_customer_action
processing
succeeded
failed
canceled
```

Refund state belongs to refund records, not to mutation of the payment attempt.

### 9.4 Mandate/payment-method status

```text
not_configured
pending
active
suspended
revoked
expired
failed
```

### 9.5 Derived platform access mode

```text
full
limited_write
read_only
billing_only
blocked
```

### 9.6 Access reason

Examples:

```text
trial_active
paid_period_active
payment_grace
trial_grace
over_limit
payment_past_due
subscription_expired
compliance_suspension
security_suspension
manual_override
```

Access mode and reason are separate. Payment resolution must not be offered as the remedy for a security suspension.

---

## 10. Access Decision Engine

### 10.1 Inputs

```text
organization operational state
active subscription contract
current effective period
dunning stage
grace timestamps
plan entitlements
usage state
compliance/security holds
approved manual overrides
current UTC timestamp
```

### 10.2 Output

```text
organization_id
mode
reason_code
effective_from
effective_until
allowed_capabilities
blocked_capabilities
available_recovery_actions
customer_message_key
projection_version
computed_at
```

### 10.3 Access projection table

```text
platform_access_projections
```

This is a read-optimized projection, not the ultimate source of truth. It is regenerated transactionally whenever relevant domain state changes and periodically reconciled.

### 10.4 Capability-based enforcement

Do not gate only by HTTP method. Define capabilities such as:

```text
platform.read_core
platform.export_data
platform.create_member
platform.update_member
platform.record_attendance
platform.create_branch
platform.manage_staff
platform.view_reports
platform.view_billing
platform.manage_billing
platform.contact_support
platform.manage_security
```

Example access matrix:

| Capability | Full | Limited write | Read only | Billing only | Blocked |
|---|---:|---:|---:|---:|---:|
| View core records | Yes | Yes | Yes | No | No |
| Export permitted data | Yes | Yes | Yes | Yes | Policy |
| Record attendance | Yes | Policy | No | No | No |
| Create members | Yes | No | No | No | No |
| Change payment method | Yes | Yes | Yes | Yes | Policy |
| View/download invoices | Yes | Yes | Yes | Yes | Policy |
| Contact support | Yes | Yes | Yes | Yes | Yes/Policy |
| Change security settings | Yes | Yes | Policy | Policy | Policy |

The exact matrix is versioned and tested.

### 10.5 Universal enforcement point

Every authenticated tenant request passes through a centralized access-policy dependency or middleware that:

1. establishes trusted tenant and actor context;
2. loads the local access projection with safe DB fallback;
3. resolves the route’s declared capability;
4. allows or rejects before business service execution;
5. records correlation ID and denial reason;
6. never trusts frontend route state.

Routes must declare capabilities explicitly. Undeclared protected routes fail closed in CI and, preferably, at application startup.

### 10.6 Structured restriction response

Recommended shape:

```json
{
  "error": {
    "code": "PLATFORM_ACCESS_READ_ONLY",
    "message": "Your account is currently read-only because the subscription has expired.",
    "reason": "subscription_expired",
    "allowed_actions": ["view_billing", "update_payment_method", "export_data"],
    "action_url": "/settings/plan-billing",
    "effective_until": null,
    "correlation_id": "..."
  }
}
```

Use a consistent HTTP authorization status and application error code. Do not rely on client support for HTTP 402 as the only signal.

---

## 11. Entitlements and Limits

### 11.1 Tables

```text
platform_features
platform_plan_entitlements
platform_entitlement_snapshots
platform_entitlement_overrides
platform_usage_snapshots
platform_capacity_reservations
```

### 11.2 Example entitlement keys

```text
branches.max_active
members.max_active
staff.max_active
membership_plans.max_active
attendance.enabled
reports.advanced.enabled
branding.custom.enabled
communications.whatsapp.enabled
api.access.enabled
audit.retention_days
storage.bytes
```

### 11.3 Entitlement value model

```text
key
value_type: boolean | integer | decimal | string | set
value
enforcement_mode: hard | soft | metered | informational
source: plan | addon | promotion | override
effective_from
effective_until
```

### 11.4 One source of truth

Application services do not authorize from:

```text
Organization.tier
Organization.max_branches
TIER_LIMITS
frontend plan labels
provider product metadata
```

They call the entitlement service.

### 11.5 Capacity enforcement

For strict limits such as branches:

1. acquire organization + entitlement scoped database/advisory lock;
2. recompute current effective quantity or lock a trusted usage row;
3. confirm capacity;
4. reserve quantity;
5. create the resource in the same transaction when possible;
6. release/consume reservation;
7. reconcile usage independently.

Two simultaneous requests must not both consume the last slot.

### 11.6 Downgrade over-limit behavior

Example:

```text
new branch limit: 2
current active branches: 5
```

Doers must:

- preserve all five branches;
- display over-limit impact before confirmation;
- prevent creation of another branch after the downgrade is effective;
- allow viewing and controlled deactivation;
- allow upgrade;
- never automatically choose customer data to delete.

---

# Part IV — Billing Ledger and Payments

## 12. Financial Ledger

### 12.1 Tables

```text
platform_invoices
platform_invoice_lines
platform_payment_attempts
platform_payment_methods
platform_mandates
platform_refunds
platform_credit_notes
platform_balance_transactions
platform_settlement_references
```

### 12.2 Invoice rules

- invoice numbers come from a controlled legal sequence;
- draft invoices may be recalculated under controlled rules;
- issued invoices are immutable;
- legal name, address, tax identity, tax treatment, rate, and rounding are snapshotted;
- correction uses voiding where permitted or explicit credit/debit note;
- invoice artifact integrity is verified by hash;
- financial retention is separate from normal tenant deletion.

### 12.3 Money rules

```text
amount_minor BIGINT
currency CHAR(3)
```

- no floating-point money;
- deterministic rounding policy;
- line totals, discounts, tax, credits, and grand totals must reconcile exactly;
- client display formatting does not alter stored values.

### 12.4 Payment attempt

A payment attempt is not an invoice and is not a subscription.

It records:

```text
invoice_id
provider
provider_payment_reference
amount_minor
currency
status
failure_category
customer_action_required
created_at
confirmed_at
```

### 12.5 Refund and credit

Refunds are append-only records linked to the original payment and invoice. They do not rewrite historical amount fields.

Large or unusual refunds require dual approval and a reason/ticket.

### 12.6 Manual/offline payment

Support bank transfer or negotiated contracts through explicit `manual` provider operations:

- proof/reference captured;
- preparer and approver separation;
- amount and invoice reconciliation;
- no free-text “mark paid” shortcut;
- immutable audit.

---

## 13. Payment Provider Boundary

### 13.1 Provider abstraction

```text
create_customer
update_customer
create_checkout_session
create_customer_portal_session
create_or_update_subscription
schedule_change
cancel_subscription
retrieve_subscription
retrieve_invoice
retrieve_payment
refund_payment
verify_webhook
normalize_event
list_reconciliation_objects
```

### 13.2 Core-provider separation

Core tables use Doers IDs. Provider identifiers belong in mapping tables such as:

```text
platform_provider_customers
platform_provider_subscriptions
platform_provider_prices
platform_provider_invoices
platform_provider_payments
```

### 13.3 Provider selection scorecard

Before selecting the India-first provider, compare:

- recurring card and UPI/e-mandate capability;
- mandate lifecycle visibility;
- retry and dunning controls;
- hosted checkout quality;
- webhook signing and replay support;
- refunds and partial refunds;
- settlement and reconciliation exports;
- tax invoice/receipt boundaries;
- sandbox fidelity;
- API and webhook versioning;
- uptime and support;
- exportability and migration path;
- total cost.

The domain model must not assume every payment method has identical recurring, retry, refund, or mandate semantics.

### 13.4 Payment-data minimization

Doers does not collect or store:

- full card number/PAN;
- CVV;
- UPI PIN;
- bank login credentials;
- complete mandate secrets.

Use provider-hosted checkout or provider-controlled payment fields. Store only references and masked metadata required for customer display and operations.

---

## 14. Webhook Inbox and Event Processing

### 14.1 Acceptance flow

1. enforce route-level payload size and content-type limits;
2. read raw body exactly once;
3. verify signature and timestamp tolerance before transformation;
4. identify provider account/environment;
5. insert raw or safely encrypted payload into durable inbox;
6. enforce unique `(provider, provider_account, provider_event_id)`;
7. return provider success after durable acceptance;
8. process asynchronously.

### 14.2 Processing flow

1. claim inbox row with bounded lease;
2. normalize provider event;
3. lock affected subscription/invoice object;
4. re-fetch provider state when ordering is ambiguous;
5. apply idempotent transition;
6. write domain event, ledger change, access projection, audit, and outbox atomically;
7. mark inbox event processed;
8. retry transient errors with backoff and jitter;
9. dead-letter permanent or unknown events;
10. alert operators when backlog or age exceeds threshold.

### 14.3 Event ordering

Provider event timestamps alone do not guarantee arrival order. Transitions use:

- provider object version when available;
- effective timestamp;
- current provider re-fetch;
- legal transition matrix;
- last-applied provider state metadata.

A stale cancellation event must not overwrite a later reactivation.

### 14.4 Payload handling

Webhook payloads:

- are never copied into ordinary logs;
- are encrypted or strongly access-restricted;
- are redacted before support display;
- follow explicit retention policy;
- are linked to correlation and processing attempts.

---

## 15. Idempotency and Concurrency

### 15.1 Required operations

- checkout session creation
- subscription creation
- plan change
- cancellation/reactivation
- payment-method change
- refund/credit
- manual-payment approval
- webhook processing
- invoice finalization
- reconciliation repair

### 15.2 Identity

Idempotency identity includes:

```text
tenant or provider account
actor or provider
operation type
client idempotency key or provider event ID
canonical request hash
```

Reusing a key with a different request hash returns conflict.

### 15.3 Correctness boundary

Use PostgreSQL uniqueness, transactions, and object-scoped locks. Redis may accelerate coordination but cannot be the only financial correctness mechanism.

### 15.4 Remote success / local timeout

When a provider call times out, the system must not blindly retry object creation. Record the outbound operation before calling the provider, use provider idempotency keys, and reconcile by operation key or provider lookup.

---

# Part V — Security Constitution

## 16. Security Baselines

The security program targets:

- OWASP ASVS 5.0.0 as application verification baseline;
- OWASP Top 10:2025 awareness coverage;
- OWASP API Security Top 10:2023 coverage;
- PCI DSS 4.0.1 scope minimization and applicable controls;
- WCAG 2.2 AA for the customer-facing experience;
- applicable CERT-In directions and incident/logging obligations;
- applicable Indian digital personal-data obligations, with legal review as commencement provisions evolve.

Compliance labels do not replace threat modeling or testing.

---

## 17. Trust Boundaries and Threat Model

Primary threat actors and failures:

- malicious tenant attempting cross-tenant object access;
- compromised owner/staff account;
- malicious or mistaken Doers internal operator;
- forged or replayed webhook;
- client tampering with price, amount, plan, tax, or entitlement;
- XSS stealing browser credentials;
- CSRF on cookie-authenticated financial actions;
- credential stuffing and session theft;
- dependency or CI supply-chain compromise;
- duplicate/out-of-order provider events;
- provider outage or API inconsistency;
- database race and double execution;
- log leakage of secrets or tax/payment data;
- backup exposure;
- denial-of-service and cost amplification;
- insider or support-tool abuse.

Each implementation phase includes an updated data-flow diagram and STRIDE-style threat review.

---

## 18. Authentication and Session Security

### 18.1 Web session decision

For the first-party Doers web application, prefer:

- Secure, HttpOnly cookies;
- appropriate SameSite policy;
- short-lived access session;
- rotating refresh session with family/reuse detection;
- CSRF protection for state-changing requests;
- explicit session revocation and device/session view.

The frontend should not retain long-lived bearer or refresh credentials in `localStorage` for privileged billing use. The current dual cookie + local-storage path must be consolidated before platform billing launch.

### 18.2 Step-up authentication

Require recent authentication and, where available, MFA for:

- changing plan with immediate charge;
- replacing payment method or mandate;
- changing legal/tax identity;
- adding/removing billing administrators;
- cancellation;
- large exports during restricted access;
- internal refunds, credits, or overrides.

### 18.3 MFA

MFA is mandatory for Doers internal operators and strongly required for organization owners and billing administrators before general availability.

### 18.4 Session defenses

- credential stuffing protection;
- progressive rate limiting;
- secure password reset;
- refresh-token rotation;
- replay/reuse detection;
- device/session revocation;
- notifications for sensitive account changes;
- no credentials in URLs or logs.

---

## 19. Tenant Isolation and Authorization

### 19.1 Trusted tenant origin

Tenant identity comes from the authenticated principal and server-established membership. Never trust arbitrary `org_id` in a body, browser-supplied tenant header, or resource UUID alone.

### 19.2 Defense layers

1. authentication;
2. tenant context;
3. function-level permission;
4. object ownership validation;
5. composite tenant foreign keys;
6. RLS where appropriate;
7. audit and anomaly detection;
8. cross-tenant automated tests.

### 19.3 Permissions

Tenant permissions:

```text
platform_billing.view
platform_billing.manage_subscription
platform_billing.manage_payment_method
platform_billing.download_invoice
platform_billing.manage_tax_profile
platform_billing.manage_billing_users
platform_billing.export_data
```

Internal-only permissions:

```text
platform_billing.issue_refund
platform_billing.issue_credit
platform_billing.override_entitlement
platform_billing.override_access
platform_billing.replay_webhook
platform_billing.reconcile
```

### 19.4 Mass-assignment protection

Client payloads can never write:

- authoritative amount;
- currency chosen outside allowed catalog;
- tax total;
- contract/access status;
- entitlement values;
- provider IDs not derived by server;
- organization ownership;
- invoice paid status;
- override approval fields.

Use explicit command schemas, not ORM model binding.

---

## 20. Internal Control Plane

Internal support tools are a separate control plane.

Requirements:

- separate operator identity and roles;
- MFA;
- just-in-time privileged access;
- ticket/reason requirement;
- short expiry;
- customer-visible or internally reviewable history where appropriate;
- dual approval above financial/entitlement thresholds;
- no direct production database editing as a normal workflow;
- break-glass procedure with automatic alert and after-action review.

Support impersonation, if ever implemented, must be visible, time-bounded, read-only by default, and comprehensively audited.

---

## 21. Application and Infrastructure Security

### 21.1 Browser and API

- strict CORS allowlist;
- CSRF defense for cookie-authenticated mutations;
- CSP with nonce/hash strategy;
- HSTS in production;
- frame-ancestors restrictions;
- content-type and payload limits;
- schema validation;
- output encoding;
- no sensitive data in query strings;
- endpoint-specific rate and cost limits;
- pagination and export limits;
- correlation IDs without leaking internals.

### 21.2 Secrets

- managed secret store, not repository or `.env` in production;
- environment/provider/account separation;
- rotation plan;
- least-privilege service credentials;
- no frontend exposure;
- no secret logging;
- webhook-secret rollover support.

### 21.3 Encryption and data protection

- TLS in transit;
- encrypted disks/backups;
- envelope/field encryption for selected sensitive tax/provider values;
- key version recorded for rotation;
- masked default presentation;
- production data not copied to development;
- sanitized provider fixtures.

### 21.4 Supply chain

- locked dependencies;
- automated dependency and license scanning;
- SAST and secret scanning;
- SBOM generation;
- minimal pinned container images;
- image vulnerability scanning and signing;
- protected branches and reviewed migrations;
- CI credentials scoped and short-lived where possible.

---

## 22. Audit, Privacy, and Retention

### 22.1 Audit events

Record:

- actor and actor type;
- organization;
- action;
- target;
- before/after safe summary;
- reason/ticket when required;
- request correlation;
- source IP/device metadata according to privacy policy;
- timestamp;
- approval chain.

Audit records are append-only and access-controlled.

### 22.2 Log hygiene

Never log:

- full card/UPI/bank data;
- access or refresh tokens;
- webhook secrets/signatures beyond safe diagnostics;
- unredacted tax identifiers;
- full raw provider payload in standard logs;
- passwords or OTPs.

### 22.3 Retention classes

Define separate schedules for:

- operational application logs;
- security logs;
- audit records;
- financial records;
- invoice artifacts;
- webhook payloads;
- support tickets;
- personal data after account closure;
- backups.

Legal/accounting/security obligations can override ordinary deletion, but retention must still be explicit and minimized.

### 22.4 Data-subject and customer operations

Design for:

- access request support;
- correction;
- account closure;
- export;
- deletion/anonymization where legally permitted;
- retention holds;
- grievance/contact process;
- vendor/subprocessor inventory.

A qualified legal/tax professional must validate the final India-specific policy before launch.

---

# Part VI — Reliability and Operations

## 23. Failure Behavior

### 23.1 Redis unavailable

- entitlement/access reads fall back to PostgreSQL;
- financial operations continue where database and provider permit;
- rate limiting degrades according to safe policy;
- no customer is suspended solely because Redis is unavailable.

### 23.2 Celery unavailable

- durable inbox/outbox accumulates safely;
- effective access remains derivable from database timestamps;
- user sees processing state;
- backlog alerts fire;
- no incorrect activation based only on frontend redirect.

### 23.3 Payment provider unavailable

- existing paid access continues based on known local contract period;
- new checkout/payment changes show temporary unavailability;
- provider outage alone does not trigger failed-payment suspension;
- outbound commands remain recoverable and reconcilable.

### 23.4 Webhook delayed or lost

- checkout shows confirming/processing;
- scheduled reconciliation retrieves provider truth;
- replay-safe repair applies missing transition;
- support can inspect operation state without editing tables.

### 23.5 Database unavailable

- privileged financial mutations fail closed;
- no false success is shown;
- last-known access cache may only be used under a tightly bounded, documented stale policy and may never elevate privilege;
- service health accurately reports degradation.

### 23.6 Notification provider unavailable

- billing state still changes correctly;
- messages remain queued with deduplication;
- notification failure never rolls back a valid financial transition;
- critical notification backlog alerts operators.

---

## 24. Reconciliation

Reconciliation is mandatory, not optional.

### 24.1 Scheduled scopes

- provider customers;
- subscriptions;
- invoices;
- payments;
- refunds;
- mandates;
- settlements;
- local entitlement/access projection.

### 24.2 Reconciliation item

```text
platform_reconciliation_runs
platform_reconciliation_items
```

Each mismatch records:

```text
object_type
local_reference
provider_reference
mismatch_type
local_snapshot
provider_snapshot
severity
repair_policy
repair_status
reviewer
```

Automatic repair is limited to safe, deterministic cases. Financially ambiguous cases require operator review.

---

## 25. Observability and Service Objectives

Recommended initial production objectives, to be validated through load tests and pilot telemetry:

- platform access-decision availability: 99.95% monthly;
- billing summary API p95: under 300 ms excluding provider calls;
- webhook durable acceptance p95: under 1 second;
- normal webhook processing p95: under 60 seconds;
- entitlement/access projection age: under 2 minutes during normal operation;
- zero known cross-tenant billing disclosures;
- zero duplicate financial effects from identical idempotency keys;
- reconciliation mismatch backlog has an explicit owner and SLA.

Key metrics:

```text
webhook_accept_latency
webhook_process_latency
webhook_oldest_unprocessed_age
provider_operation_unknown_count
checkout_success_rate
payment_success_rate_by_method
payment_action_required_rate
dunning_recovery_rate
access_projection_age
access_denial_rate_by_reason
erroneous_denial_reports
idempotency_conflicts
reconciliation_mismatch_count
refund_approval_latency
invoice_generation_failures
notification_backlog_age
```

Alerts must be actionable and linked to runbooks.

---

## 26. Backup, Restore, and Disaster Recovery

- point-in-time database recovery;
- encrypted offsite backups;
- invoice artifact backup and integrity verification;
- webhook inbox/outbox included;
- provider mapping and reconciliation history included;
- documented RPO/RTO;
- quarterly restore exercise before scale, with evidence;
- post-restore provider reconciliation before normal billing mutations resume;
- no assumption that provider can reconstruct all local legal/audit state.

Recommended initial targets for the billing control plane:

```text
RPO: 5 minutes or better
RTO: 60 minutes or better
```

Targets are operational commitments only after infrastructure and drills prove them.

---

## 27. Operational Runbooks

Required before launch:

- provider outage;
- webhook backlog;
- duplicate charge complaint;
- payment succeeded but account not activated;
- account suspended by mistake;
- mistaken entitlement limit;
- refund and partial refund;
- credit note / invoice correction;
- tax-profile correction;
- compromised provider key;
- webhook secret rotation;
- database restore;
- reconciliation mismatch;
- internal operator misuse;
- data export during billing-only access;
- customer cancellation dispute.

---

# Part VII — API and Code Structure

## 28. Backend Package Structure

```text
app/platform_billing/
    __init__.py
    domain/
        enums.py
        commands.py
        events.py
        errors.py
        money.py
        policies.py
        state_machine.py
        access_decision.py
        entitlement.py
    models/
        catalog.py
        billing_account.py
        subscription.py
        entitlement.py
        access_projection.py
        invoice.py
        payment.py
        provider.py
        webhook.py
        reconciliation.py
    repositories/
        catalog_repository.py
        billing_account_repository.py
        subscription_repository.py
        entitlement_repository.py
        ledger_repository.py
        provider_repository.py
        webhook_repository.py
        reconciliation_repository.py
    services/
        catalog_service.py
        billing_account_service.py
        subscription_service.py
        entitlement_service.py
        access_service.py
        checkout_service.py
        invoice_service.py
        payment_service.py
        dunning_service.py
        reconciliation_service.py
    providers/
        base.py
        registry.py
        <provider_name>/
    policies/
        authorization.py
        trial.py
        plan_change.py
        dunning.py
        cancellation.py
        refund.py
    api/
        schemas.py
        tenant_router.py
        webhook_router.py
        internal_router.py
    tasks/
        webhook_processor.py
        outbox_dispatcher.py
        reconciliation.py
        dunning.py
        notifications.py
```

Domain logic must not import FastAPI, frontend types, or provider SDK objects.

---

## 29. Tenant API Surface

```text
GET  /organizations/{org_id}/platform-billing/summary
GET  /organizations/{org_id}/platform-billing/plans
GET  /organizations/{org_id}/platform-billing/plans/{plan_version_id}
POST /organizations/{org_id}/platform-billing/checkout-sessions
GET  /organizations/{org_id}/platform-billing/subscription
POST /organizations/{org_id}/platform-billing/subscription/changes
POST /organizations/{org_id}/platform-billing/subscription/cancel
POST /organizations/{org_id}/platform-billing/subscription/reactivate
GET  /organizations/{org_id}/platform-billing/invoices
GET  /organizations/{org_id}/platform-billing/invoices/{invoice_id}
GET  /organizations/{org_id}/platform-billing/invoices/{invoice_id}/artifact
POST /organizations/{org_id}/platform-billing/customer-portal-session
GET  /organizations/{org_id}/platform-billing/tax-profile
PATCH /organizations/{org_id}/platform-billing/tax-profile
GET  /organizations/{org_id}/platform-billing/history
GET  /organizations/{org_id}/entitlements
GET  /organizations/{org_id}/access-state
```

Mutations require `Idempotency-Key`, recent-auth context where applicable, and explicit permission.

### 29.1 Summary response

The frontend should obtain one coherent read model:

```text
access state
current subscription
current plan
next billing date
trial/grace dates
usage and limits
payment/mandate display status
upcoming invoice estimate
available actions
notices
projection version
```

This prevents many inconsistent calls and UI races.

---

## 30. Provider Webhook API

```text
POST /webhooks/platform-billing/{provider}
```

This endpoint:

- is not tenant-authenticated;
- uses provider signature verification;
- has strict size, timeout, and rate protections;
- writes durable inbox only;
- returns no sensitive processing detail.

---

## 31. Internal Operations API

Separate protected namespace:

```text
/internal/platform-billing/...
```

Examples:

- inspect provider operation;
- replay dead-letter event;
- run reconciliation;
- approve refund;
- create time-bounded access override;
- inspect audit history.

Not exposed through tenant roles.

---

## 32. Frontend Feature Structure

```text
src/features/platformBilling/
    api/
    components/
    hooks/
    pages/
    schemas/
    store/
    types/
    utils/

src/pages/settings/plan-billing/
src/pages/account-recovery/billing/
```

Use server-derived types or schema validation for critical responses. Avoid duplicating lifecycle logic in React components.

---

# Part VIII — Lifecycle Policies

## 33. Recommended Initial Customer Policy

These are recommended defaults for an India-first launch. They remain configurable through immutable policy versions and require commercial approval.

### 33.1 Trial

- 14 days;
- no payment method required initially;
- one standard self-service trial per organization/legal customer, with controlled sales override;
- all important features enabled except abuse-prone or cost-heavy features if necessary;
- clear trial end date shown from day one.

### 33.2 Trial expiry

Recommended:

```text
Day 0: trial ends; 3-day full-access grace with prominent notice
Day 3: read-only for 7 additional days
Day 10: billing-only recovery access
```

No data deletion is tied to these dates.

### 33.3 Renewal failure

Recommended:

```text
Initial failure: full access + notice + customer action
Retry window: provider/policy driven
Later stage: limited-write or read-only
Final stage: billing-only
```

Do not suspend because the provider API is unreachable or a webhook is merely delayed.

### 33.4 Upgrade

- effective immediately by default;
- exact charge/credit preview before confirmation;
- provider/customer action may create an awaiting-payment state;
- entitlements increase only after policy-defined confirmation.

### 33.5 Downgrade

- effective at next renewal by default;
- impact preview required;
- scheduled change is reversible before effective time;
- existing excess data is preserved;
- new creation is restricted after effective time until compliant.

### 33.6 Cancellation

- cancel at period end by default;
- no intentionally difficult cancellation flow;
- reactivation available until effective boundary when provider permits;
- immediate cancellation is a privileged exception with explicit refund/credit policy.

### 33.7 Refund

- written policy by plan and jurisdiction;
- no silent ad-hoc refund promises in UI;
- partial/full refund represented explicitly;
- dual approval above threshold;
- customer receipt and ledger reconciliation.

---

# Part IX — Testing Constitution

## 34. Domain Tests

- every valid/invalid state transition;
- boundary timestamps and timezone display;
- trial conversion;
- renewal and grace;
- upgrade/downgrade proration policy;
- cancellation/reactivation races;
- pause/resume if supported;
- over-limit behavior;
- refund/credit math;
- invoice rounding and totals;
- policy-version retention.

Use property-based tests for money invariants and state-machine legality where practical.

---

## 35. Security Tests

For every endpoint:

- different organization ID in path;
- foreign object ID;
- altered provider ID;
- missing/incorrect permission;
- staff versus owner role;
- mass assignment;
- stale/revoked session;
- CSRF;
- IDOR/BOLA;
- excessive data exposure;
- rate/cost abuse;
- export authorization.

Webhook tests:

- invalid signature;
- stale timestamp;
- replay;
- duplicate event;
- malformed JSON;
- oversized payload;
- unknown event;
- wrong environment/account;
- payload redaction.

Internal control-plane tests:

- MFA/step-up;
- approval threshold;
- expiring override;
- audit completeness;
- break-glass alert.

---

## 36. Reliability Tests

- duplicate checkout click;
- provider timeout after remote success;
- duplicate webhook;
- out-of-order webhook;
- lost webhook repaired by reconciliation;
- worker crash after inbox insert;
- worker crash after provider operation but before local commit;
- database deadlock/retry;
- Redis unavailable;
- Celery unavailable;
- notification provider unavailable;
- provider unavailable during renewal;
- stale access cache;
- backup restore and post-restore reconciliation.

---

## 37. UX Tests

- no redirect loops;
- no repeated banner after snooze;
- warning frequency caps;
- draft preservation after restriction;
- plan change impact is understandable;
- payment processing does not show false success;
- keyboard-only completion;
- screen-reader error and status announcements;
- mobile layout;
- slow network and retry behavior;
- double-click protection;
- cancellation and downgrade are discoverable.

Conduct moderated usability tests with actual gym/studio owners before general availability.

---

## 38. Provider Contract Tests

- signed webhook fixture validation;
- provider idempotency behavior;
- customer/session creation;
- mandate states;
- recurring payment states;
- partial/full refunds;
- subscription change;
- cancellation;
- pagination;
- API version compatibility;
- settlement/reconciliation retrieval.

Sanitized fixtures are versioned. Sandbox behavior is not assumed identical to production; pilot monitoring remains mandatory.

---

# Part X — Migration and Implementation Plan

## 39. Phase 0 — Freeze Product and Policy

Deliverables:

- plan/price matrix;
- entitlement catalog;
- trial policy;
- grace/dunning timeline;
- upgrade/downgrade rules;
- cancellation/refund rules;
- India billing/tax ownership decision;
- access capability matrix;
- operator approval matrix;
- data-retention classes;
- provider scorecard;
- threat model;
- ADRs.

No migration code before these are reviewed.

---

## 40. Phase 1 — Additive Read-Only Foundation

Implement without live payment mutations:

- `app/platform_billing/` domain;
- catalog and immutable plan versions;
- subscription read models;
- entitlement resolver;
- access-decision engine;
- access projection;
- tenant constraints, RLS, audit;
- summary/access/entitlement GET APIs;
- test fixtures and state-machine tests.

Keep existing trial/tier system active during shadow comparison.

---

## 41. Phase 2 — Shadow Evaluation and Enforcement Migration

- compute new access decisions without enforcing;
- compare with existing trial/tier outcome;
- resolve discrepancies;
- map existing organizations and trials to new contract/period records;
- introduce route capability declarations;
- enforce on pilot routes;
- replace direct `Organization.tier`, `TIER_LIMITS`, and `max_branches` authorization;
- retain a temporary compatibility projection only where old code still requires it;
- add universal enforcement.

The database branch-limit mechanism must eventually read the centralized entitlement source or be replaced with an equivalent transactionally safe capacity guard.

---

## 42. Phase 3 — Frontend Clarity Before Payments

- rename menu labels to Member Subscriptions and Member Payments & Collections;
- add Plan & Billing route and summary UI;
- replace `/subscription-required` destination;
- implement one status banner system;
- replace technical/poetic critical billing copy with plain language;
- implement billing recovery shell;
- remove frontend lifecycle calculations;
- consolidate session credential strategy.

This phase can run against test catalog and manual subscription fixtures.

---

## 43. Phase 4 — Provider Adapter and Checkout

- select one provider through approved scorecard;
- hosted checkout/customer portal;
- provider mappings;
- outbound operation journal;
- signed webhook inbox;
- asynchronous processing;
- reconciliation;
- processing UX;
- provider contract tests.

No automatic suspension until reconciliation and dunning behavior are proven.

---

## 44. Phase 5 — Ledger, Invoices, Dunning, Refunds

- invoice and line snapshots;
- payment attempts and mandates;
- retry/dunning policy;
- customer notifications;
- refund/credit workflow;
- tax profile and invoice artifacts;
- internal operator tooling;
- settlement reconciliation.

---

## 45. Phase 6 — Security and Reliability Hardening

- ASVS-based verification matrix;
- API security review;
- penetration test by qualified independent party;
- tenant-isolation test suite;
- load and chaos tests;
- backup/restore drill;
- provider outage drill;
- secret rotation drill;
- accessibility audit;
- privacy/retention review;
- runbook exercises.

---

## 46. Phase 7 — Controlled Rollout

1. internal test organizations;
2. selected friendly pilot customers;
3. payment confirmation and reconciliation observation;
4. limited percentage rollout;
5. full rollout only after launch gates;
6. rollback path remains available throughout.

Feature flags control enforcement and provider operations independently.

---

# Part XI — Exact Repository Direction

## 47. Backend Integration Direction

### Add

```text
app/platform_billing/**
new additive Alembic migrations
platform-billing router registrations
tasks for webhook, outbox, reconciliation, dunning
capability registry and enforcement dependency
```

### Deprecate gradually

```text
Organization.tier as authorization source
Organization.max_branches as authorization source
TIER_LIMITS as authorization source
TrialSubscription as permanent contract source
require_trial_active as final access gate
```

### Keep

```text
existing member subscription and payment contexts
DB-backed idempotency foundation, after billing-specific review
audit/outbox infrastructure, extended where needed
tenant/RLS foundations
```

### Correct before launch

- secure web-session strategy must be singular and tested;
- route capability declaration must be comprehensive;
- trial/access state must be server-enforced;
- billing recovery routes must remain reachable during restriction;
- Redis/Celery failures must not corrupt access truth.

---

## 48. Frontend Integration Direction

### Add

```text
src/features/platformBilling/**
src/pages/settings/plan-billing/**
src/pages/account-recovery/billing/**
canonical platform access store sourced from backend summary
```

### Rename for users

```text
Subscriptions -> Member Subscriptions
Payments/Billing -> Member Payments & Collections
```

### Replace

- `TrialLockBanner` with a general `PlatformBillingStatusBanner`;
- hard-coded `/subscriptions` recovery links with `/settings/plan-billing` or recovery route;
- interceptor-driven unconditional navigation with state-aware route handling;
- generic “hard locked” copy with precise, customer-friendly language.

### Security migration

Remove persistent browser-readable refresh credentials from the privileged billing session path after the backend cookie/CSRF model is completed and tested.

---

# Part XII — Launch Gates

## 49. Mandatory Acceptance Gates

Production platform billing is not approved until all are true:

### Domain separation

- no platform financial record uses member subscription/payment tables;
- routes and UI clearly distinguish both domains.

### Authorization

- no platform authorization reads `Organization.tier` directly;
- all protected routes declare a capability;
- every billing endpoint passes cross-tenant tests;
- frontend is not an enforcement boundary.

### Financial correctness

- all mutations are idempotent;
- concurrent last-slot capacity tests pass;
- invoice totals and rounding reconcile;
- duplicate events cannot duplicate financial effects;
- issued records are immutable.

### Provider reliability

- webhook replay and out-of-order tests pass;
- reconciliation repairs missed events;
- provider timeout after remote success is recoverable;
- provider outage does not wrongly suspend active customers.

### Security

- payment secrets never enter Doers logs/storage;
- session and CSRF model is approved;
- step-up and MFA policy is operational;
- secrets manager and rotation are tested;
- internal overrides expire and are audited;
- independent security review completed.

### UX

- no redirect loops;
- no false payment-success state;
- cancellation and downgrade are clear;
- restricted users retain permitted billing/export/support access;
- draft preservation works;
- WCAG 2.2 AA audit issues are resolved or formally risk-accepted.

### Operations

- dashboards and alerts exist;
- runbooks exercised;
- backup restore tested;
- reconciliation ownership assigned;
- incident response and customer communication process exists.

---

# Part XIII — Decisions to Freeze Before Phase 1

## 50. Commercial Decisions

1. plan names and target segments;
2. monthly, annual, or both;
3. exact price and currency;
4. trial length and card requirement;
5. entitlement list and limits;
6. add-ons;
7. promotions and coupons;
8. upgrade proration;
9. downgrade timing;
10. cancellation/refund policy.

## 51. Operational Decisions

1. grace and dunning stages;
2. notification channels and frequency caps;
3. recovery access matrix;
4. data export after expiry;
5. manual contracts/bank transfer;
6. support override thresholds;
7. refund approval thresholds;
8. RPO/RTO;
9. log and financial retention;
10. incident escalation.

## 52. India-Specific Decisions

1. legal invoicing entity;
2. GST registration and invoice sequence ownership;
3. tax-inclusive versus exclusive pricing;
4. selected provider and recurring methods;
5. mandate and retry behavior;
6. settlement reconciliation process;
7. invoice/receipt responsibilities between Doers and provider;
8. legal review of customer terms, privacy, cancellation, and retention.

---

# Final Constitutional Statement

Doers Platform Billing is not a payment button and not a trial banner. It is a commercial contract system, authorization control plane, financial ledger, provider-integration boundary, and customer recovery experience.

The system is considered high quality only when all of these are simultaneously true:

```text
secure enough that tenant and financial boundaries cannot be bypassed;
reliable enough that retries and outages do not corrupt state;
clear enough that owners understand every charge and restriction;
calm enough that warnings do not interrupt ordinary work;
recoverable enough that payment problems do not become data-loss events;
auditable enough that every sensitive decision can be explained.
```

This V2 constitution supersedes the V1 baseline wherever they differ. Implementation must proceed phase by phase, with verification evidence and a review checkpoint before the next phase.