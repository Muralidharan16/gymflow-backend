# Subscription Lifecycle Phase 0 Audit

Date: 2026-06-15

## Scope

This audit covers the current backend and frontend subscription implementation before introducing a parent/term lifecycle model. No schema or API changes are included in this phase.

## Backend Current State

### Modern subscription tables

Current modern subscription tables are:

- `member_subscriptions_v2`
- `subscription_members`

`member_subscriptions_v2` currently behaves as a flat subscription term table. Each admission creates one row with:

- `org_id`
- `branch_id`
- `membership_plan_id`
- `primary_member_id`
- `subscription_code`
- `start_date`
- `end_date`
- `status`
- plan price/duration/capacity snapshots
- `cancelled_at`
- `archived_at`

`subscription_members` stores current slots for a subscription row:

- `subscription_id`
- `member_id`
- `slot_number`
- `role`
- `is_active`
- `joined_at`
- `left_at`

It does not preserve slot assignment history beyond the current row.

### Modern statuses

Modern subscription status values are:

- `pending`
- `active`
- `expired`
- `cancelled`
- `frozen`
- `archived`

There is no explicit state machine service enforcing allowed transitions.

### Current modern API contracts

Current endpoints:

- `POST /organizations/{org_id}/member-subscriptions`
- `GET /organizations/{org_id}/member-subscriptions`
- `GET /organizations/{org_id}/member-subscriptions/{subscription_id}`

Create request:

```json
{
  "branch_id": "uuid",
  "membership_plan_id": "uuid",
  "primary_member_id": "uuid",
  "start_date": "YYYY-MM-DD"
}
```

List query parameters currently supported:

- `page`
- `page_size`
- `status`
- `branch_id`
- `member_id`

The frontend type currently includes `membership_plan_id` and `search`, but the backend list route does not support them yet.

### Current create/admission behavior

`MemberSubscriptionV2Service.create_subscription`:

- validates organization, branch, member, and plan ownership
- validates member profile status is active
- validates plan status is active
- prevents active/frozen primary-member overlap through repository check
- snapshots plan price, currency, duration, and capacity
- creates the subscription row as `active`
- creates one primary slot

It does not:

- create or link a parent subscription series
- record renewal lineage
- use idempotency keys
- use row/advisory locks for duplicate renewal protection
- create audit/timeline events
- create invoices/payments
- support scheduled renewals
- expose freeze/resume/archive operations in the modern API

### Legacy subscription/payment behavior

Legacy tables still exist:

- `subscription_plans`
- `member_subscriptions`
- `member_freeze_logs`
- `payments`
- `invoices`

Legacy `SubscriptionService` can:

- assign a plan to a member
- create a payment
- generate an invoice
- freeze/unfreeze a subscription
- cancel a subscription

Legacy implementation is gym-scoped, not the modern org-scoped subscription path. It mutates member status during freeze/cancel, which conflicts with the newer product rule that member profile status and subscription status are separate concepts.

### Payment linkage

Current `payments` and `invoices` reference legacy `member_subscriptions.id`, not `member_subscriptions_v2.id`.

Modern subscriptions currently have no payment or invoice linkage.

### Audit, RLS, idempotency, and locking infrastructure

Available infrastructure:

- `app/core/idempotency.py`
- `app/core/advisory_locks.py`
- `app/services/audit_service.py`
- transactional outbox models/tasks
- branch lifecycle event patterns
- multiple RLS/audit migrations for branch/contact/address domains

Modern subscriptions do not currently use this infrastructure.

### Current migration risks

The modern subscription migration uses `ON DELETE CASCADE` for:

- `member_subscriptions_v2.org_id -> organizations.id`
- `subscription_members.org_id -> organizations.id`
- `subscription_members.subscription_id -> member_subscriptions_v2.id`

For financial/history-grade subscription lifecycle, future tables should avoid destructive cascade on historical records and use `RESTRICT` where required by policy.

Existing modern records are flat terms. Grouping them into series must be conservative:

- use explicit lineage where available
- otherwise avoid guessing
- produce review output for ambiguous same-member records

## Frontend Current State

### Files inspected

- `src/pages/subscriptions/page.tsx`
- `src/features/subscriptions/types/subscription.ts`
- `src/features/subscriptions/services/subscriptionsApi.ts`
- `src/features/subscriptions/hooks/useSubscriptions.ts`
- member data-layer files used for admission/member rendering

### Current subscription page behavior

The page currently:

- uses flat `MemberSubscriptionV2` records
- lists each subscription term as its own row
- shows search and client-side filters
- defaults status filter to active
- defaults branch filter to current branch
- uses all active members for row display
- uses unsubscribed active members for admission selector
- has a visible `Renew` action that opens the admission modal prefilled
- has a disabled `Freeze` button

The current `Renew` flow is not a real renewal. It reuses the create/admission payload and endpoint, so it creates a new independent subscription row with no lineage.

### Current frontend API contract

Frontend calls:

- `GET /organizations/{orgId}/member-subscriptions`
- `POST /organizations/{orgId}/member-subscriptions`

Frontend normalizes Decimal price strings to numbers.

The frontend model treats `subscription_members.created_at` as optional because the backend response currently omits it from `SubscriptionMemberResponse`.

## Gap Analysis Against Target Specification

Missing backend domain objects:

- subscription series
- subscription terms as separate explicit table/entity
- term slots
- slot assignment history
- freeze history for modern subscriptions
- subscription events/timeline
- idempotency records specific to renewal response replay
- renewal lineage fields

Missing backend behavior:

- transactional renewal service
- early renewal scheduling
- duplicate-renewal prevention
- overlap constraints
- explicit state machine
- archive/restore policy
- freeze/resume policy for modern subscriptions
- modern payment linkage
- available actions projection
- current/history/all-record views

Missing frontend behavior:

- Current/Upcoming/History/Archived/All Records tabs
- one operational row per series in Current
- expandable term history
- details drawer/page
- guided renewal modal
- action matrix based on backend policy
- URL-synced filters
- server-side search/filter/pagination for large orgs
- lineage display

## Recommended Implementation Phases

### Phase 1: Domain model and migration design

Design the target schema in detail before writing migrations:

- `subscription_series`
- `subscription_terms`
- `subscription_term_slots`
- `subscription_slot_assignments`
- `subscription_freezes`
- `subscription_events`
- renewal idempotency strategy

Define status enums, transition rules, and compatibility mapping from `member_subscriptions_v2`.

### Phase 2: Additive migrations and backfill

Use additive migrations first. Preserve all existing `member_subscriptions_v2` rows as historical-compatible terms or compatibility records.

Do not drop or rewrite existing rows until compatibility has been proven.

### Phase 3: Backend query/read model

Add series/term repositories and read endpoints that support:

- current
- upcoming
- history
- archived
- all

### Phase 4: Renewal service

Implement transactional renewal with:

- row/advisory locking
- idempotency key
- source term validation
- overlap prevention
- lineage
- audit event

### Phase 5: Modern freeze/archive services

Implement freeze history, resume, archive, restore, and available actions.

### Phase 6: Frontend data layer

Add new types and API clients while keeping compatibility with existing subscription list until backend migration is complete.

### Phase 7: Frontend UX

Build tabs, series rows, history, details, renewal modal, and action matrix.

## Immediate Caution

The current frontend Renew action should be treated as a temporary shortcut only. It does not satisfy the lifecycle specification because it does not create lineage or idempotently link a new term to a previous term.

Do not build payments on top of the current flat modern subscription structure until the series/term model is designed.
