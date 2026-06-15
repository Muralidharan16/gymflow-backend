# Subscription Lifecycle Phase 2 Migration Report

Date: 2026-06-15

Branch: `feature/subscription-lifecycle-phase-2`

Baseline commit: `3e61e20dffcffdbd898734fd448e479e2d5ee13a`

## Scope

Phase 2 implemented the database foundation for the new subscription lifecycle model only.

Included:

- Alembic migrations for lifecycle schema creation.
- Conservative backfill from `member_subscriptions_v2`.
- Database constraints, exclusion rules, and trigger-based integrity checks.
- Migration regression tests.

Excluded:

- No ORM application models.
- No repositories.
- No services.
- No routers or APIs.
- No frontend changes.
- No payments, invoices, or payment allocation logic.
- No mutation of legacy `member_subscriptions`.

## Migrations Added

### `c3a4b5c6d7e8_create_subscription_lifecycle_foundation.py`

Creates the subscription lifecycle tables and PostgreSQL enum types:

- `subscription_series`
- `subscription_terms`
- `subscription_term_slots`
- `subscription_slot_assignments`
- `subscription_freezes`
- `subscription_events`
- `subscription_operation_idempotency`

Important choices:

- `member_subscriptions_v2` remains intact and is not renamed.
- `subscription_series` is the long-lived relationship identity.
- `subscription_terms` stores each dated entitlement period.
- `subscription_term_slots` stores capacity slots.
- `subscription_slot_assignments` stores member occupancy history.
- `subscription_events` is an append-only operational timeline table, not event sourcing.
- Historical lifecycle rows use `ON DELETE RESTRICT`.
- Actor columns are nullable UUIDs because no final organization-user FK is stable yet.

### `d4e5f6a7b8c9_backfill_subscription_lifecycle_from_v2.py`

Backfills lifecycle data conservatively from `member_subscriptions_v2`.

Backfill policy:

- One `subscription_series` per existing `member_subscriptions_v2` row.
- One `subscription_terms` row per existing `member_subscriptions_v2` row.
- No inferred renewal lineage.
- No grouping of adjacent subscriptions.
- No freeze history is invented.
- Source v2 rows and `subscription_members` rows are preserved.

Backfilled source identity:

- `subscription_series.metadata.source_table = member_subscriptions_v2`
- `subscription_series.metadata.source_id = <v2 id>`
- `subscription_terms.legacy_member_subscription_v2_id = <v2 id>`
- `subscription_terms.legacy_subscription_code = <v2 subscription_code>`

Backfilled snapshot fields:

- plan code/name
- duration value/unit
- capacity
- currency
- list/final price
- source status metadata

Preconditions:

- Source subscriptions cannot exceed snapshot capacity.
- Source slot numbers must fit within snapshot capacity.
- Each source subscription must have a primary slot row.

Downgrade behavior:

- Deletes only lifecycle rows created by the migration.
- Preserves `member_subscriptions_v2`.
- Preserves `subscription_members`.
- Uses temporary migrated-term and migrated-series ID tables so cleanup order is deterministic.

### `e5f6a7b8c9d0_harden_subscription_lifecycle_constraints.py`

Adds database hardening:

- One normal renewal child per parent term for active/scheduled/pending-payment renewal rows.
- One primary slot per term.
- No overlapping reserving terms in a series for `scheduled` or `active` terms.
- No overlapping non-voided assignments within the same term slot.
- No overlapping scheduled/active freezes for the same term.
- Tenant integrity trigger for `subscription_series.primary_member_id`.
- Tenant and lineage trigger for `subscription_terms.plan_id` and `renewed_from_term_id`.
- Assignment integrity trigger for member org, slot/term match, and assignment date window.
- Freeze integrity trigger for term/series match and freeze date window.

Overlap date convention:

- Business dates are inclusive.
- PostgreSQL exclusion ranges use half-open ranges:
  `daterange(starts_on, effective_ends_on + 1, '[)')`

Term reservation policy:

- `scheduled` and `active` terms reserve entitlement dates.
- `pending_payment` does not reserve overlap in Phase 2. This keeps unpaid terms from blocking valid active access.

## Tests Added

Added:

- `tests/test_subscription_lifecycle_migrations.py`

Coverage:

- Downgrade to Phase 1 baseline.
- Seed representative v2 subscriptions.
- Upgrade to Phase 2 head.
- Verify conservative backfill row counts.
- Verify source v2 tables are preserved.
- Verify adjacent same-member subscriptions remain separate series.
- Verify family capacity creates vacant slots.
- Verify legacy subscription codes and snapshot fields are preserved.
- Verify downgrade removes lifecycle rows while preserving v2 source rows.
- Verify upgrade after downgrade is repeatable.
- Verify invalid dates, negative amounts, term overlaps, cross-series renewal lineage, slot assignment overlap, and out-of-term assignment dates are rejected.
- Verify adjacent renewal dates are allowed.

Representative migration-test row counts:

```text
member_subscriptions_v2:          4
subscription_members:             5
subscription_series:              4
subscription_terms:               4
subscription_term_slots:          6
subscription_slot_assignments:    5
renewed_from_term_id backfilled:  0
```

## Verification

Environment:

```text
Database: gymflow_test
App database env: gymflow
Pytest command env: TEST_DATABASE_URL required
```

Alembic:

```text
alembic heads:   e5f6a7b8c9d0 (head)
alembic current: e5f6a7b8c9d0 (head)
```

Direct migration verification:

```text
alembic downgrade b1c2d3e4f5a6: passed
alembic upgrade head:           passed
```

Targeted migration tests:

```text
tests/test_subscription_lifecycle_migrations.py
2 passed, 2 warnings
```

Focused regression suite:

```text
tests/test_subscription_lifecycle_migrations.py
tests/test_members.py
tests/test_membership_plans.py
tests/test_member_subscriptions_v2.py
tests/test_branch_lifecycle.py
tests/test_branch_management.py
tests/test_branch_contacts_api.py
tests/test_branch_operating_hours_api.py

83 passed, 141 warnings
```

Full backend suite:

```text
174 passed, 1 failed, 3 errors, 197 warnings
```

Known baseline failures/errors remained:

```text
FAILED tests/test_rbac_phases_15_to_19.py::test_audit_key_registry_bootstrap
ERROR  tests/test_staff_roles.py::test_organization_user_flow
ERROR  tests/test_staff_roles.py::test_branch_staff_role_assignment
ERROR  tests/test_staff_roles.py::test_access_control_via_dependency
```

Observed signatures:

- Audit key registry expects `alias/gymflow-audit-v1`, but database contains `local/audit-signing-key-v1`.
- Staff role setup has Redis event-loop reuse issue.
- Staff role setup expects partition table `public.branch_audit_log_y2026_m05`, which is absent.

These match the known unrelated Phase 1.5 baseline failure area and are not introduced by Phase 2 lifecycle migrations.

Syntax check:

```text
syntax ok: 4 files
```

## Operational Notes

Production/pre-production rollout should treat this as a structural lifecycle migration:

1. Back up the database before applying Phase 2.
2. Run precondition checks from the backfill migration before deployment.
3. Confirm `member_subscriptions_v2` and `subscription_members` row counts.
4. Apply migrations in order.
5. Verify lifecycle row counts reconcile with v2 source counts.
6. Verify sample subscriptions for individual and family/capacity plans.
7. Do not switch APIs to the lifecycle tables until Phase 3+ service/API work is implemented and separately tested.

Rollback:

- Downgrading to `b1c2d3e4f5a6` removes lifecycle tables and enum types.
- Existing v2 subscription data remains available.
- Rollback does not attempt to reverse-infer renewals or restore data into v2, because v2 is never mutated by these migrations.

## Final Verdict

```text
Phase 2 database foundation: IMPLEMENTED
Legacy v2 preserved:         YES
Renewal lineage inferred:    NO
Frontend touched:            NO
APIs/services touched:       NO
Focused regression:          PASSED
Full suite:                  Known unrelated baseline failures remain
Safe for review:             YES
Safe to commit:              YES, after human review of the migration diff
Safe to start APIs/UI:        NO, wait for Phase 2 acceptance
```
