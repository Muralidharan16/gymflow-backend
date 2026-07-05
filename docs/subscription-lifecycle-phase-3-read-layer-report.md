# Subscription Lifecycle Phase 3 Read-Layer Report

## Scope

Phase 3 adds an application-layer read foundation for the subscription lifecycle tables created in Phase 2.

This phase is read-only. It does not add renewal, freeze, archive, payment, route cutover, frontend wiring, or background expiry behavior.

## Files Added

- `app/domain/__init__.py`
- `app/domain/subscription_lifecycle.py`
- `app/models/subscription_lifecycle.py`
- `app/repositories/subscription_lifecycle_repo.py`
- `app/services/subscription_lifecycle_query_service.py`
- `app/schemas/subscription_lifecycle.py`
- `tests/test_subscription_lifecycle_status.py`
- `tests/test_subscription_lifecycle_models.py`
- `tests/test_subscription_lifecycle_repositories.py`
- `docs/subscription-lifecycle-phase-3-read-layer-report.md`

## Files Changed

- `app/models/__init__.py`

## Domain Layer

Added Python enums matching the Phase 2 PostgreSQL enum values:

- `subscription_series_status`
- `subscription_term_status`
- `subscription_term_source`
- `subscription_slot_role`
- `subscription_assignment_state`
- `subscription_freeze_status`
- `subscription_event_type`
- idempotency processing states

Added display/read dataclasses:

- `MemberBrief`
- `BranchBrief`
- `FreezeSummary`
- `TermSummary`
- `SlotSummary`
- `TimelineItem`
- `SeriesSummary`
- `LifecycleV2Projection`

Added domain errors for read-layer integrity cases:

- missing series or term
- tenant mismatch
- multiple current terms
- invalid lifecycle data
- unsupported status
- corrupt renewal lineage
- slot assignment integrity

Added read-only lifecycle helpers:

- timezone-aware business-date helper
- derived term status resolver
- active-freeze resolver
- available-action calculator

## ORM Layer

Added SQLAlchemy mappings for all seven Phase 2 lifecycle tables:

- `subscription_series`
- `subscription_terms`
- `subscription_term_slots`
- `subscription_slot_assignments`
- `subscription_freezes`
- `subscription_events`
- `subscription_operation_idempotency`

Important mapping notes:

- PostgreSQL enum names match Phase 2 exactly.
- Composite tenant foreign keys are represented in the ORM.
- JSONB columns named `metadata` are mapped as `metadata_json` to avoid SQLAlchemy reserved-name conflicts.
- Relationship mappings are view-only where composite tenant keys overlap, matching Phase 3's read-only purpose.
- Existing legacy subscription models and v2 subscription models were not changed.

## Repository Layer

Added `SubscriptionLifecycleRepository` with tenant-scoped read methods:

- `get_series_by_id(org_id, series_id)`
- `get_term_by_id(org_id, term_id)`
- `list_series_summaries(...)`
- `get_series_detail(...)`
- `list_upcoming_terms(...)`
- `list_history_terms(...)`
- `list_all_terms(...)`
- `list_archived_series(...)`
- `list_slots(...)`
- `list_timeline(...)`
- `get_v2_projection(...)`

Read behavior implemented:

- one summary row per series
- current-term resolution from dates and stored status
- upcoming term resolution, including future-starting migrated `active` rows
- history term reads
- archived series reads
- slot occupancy reads
- active freeze display state
- timeline pagination
- available actions for display
- v2 compatibility projection from lifecycle data

Every public repository method requires `org_id`.

## Service and Schema Layer

Added `SubscriptionLifecycleQueryService` as a thin read-only wrapper around the repository.

Added Pydantic response schemas for future API route adoption. No routes were changed in this phase.

## Verification

Focused Phase 3 tests:

```text
tests/test_subscription_lifecycle_status.py
tests/test_subscription_lifecycle_models.py
tests/test_subscription_lifecycle_repositories.py

Result: 10 passed
```

Phase 2 migration regression:

```text
tests/test_subscription_lifecycle_migrations.py

Result: 2 passed
```

Combined lifecycle suite:

```text
tests/test_subscription_lifecycle_status.py
tests/test_subscription_lifecycle_models.py
tests/test_subscription_lifecycle_repositories.py
tests/test_subscription_lifecycle_migrations.py

Result: 12 passed
```

Existing v2/member regression:

```text
tests/test_member_subscriptions_v2.py
tests/test_members.py

Result: 45 passed
```

Full backend suite, sequential:

```text
pytest -q

Result: 184 passed, 1 failed, 3 errors
Allowed known failures:
- tests/test_rbac_phases_15_to_19.py::test_audit_key_registry_bootstrap
- tests/test_staff_roles.py::test_organization_user_flow
- tests/test_staff_roles.py::test_branch_staff_role_assignment
- tests/test_staff_roles.py::test_access_control_via_dependency
```

No subscription lifecycle, member, member-subscription-v2, migration, or import failures appeared in the full-suite run.

Import check:

```text
app.domain.subscription_lifecycle
app.models.subscription_lifecycle
app.repositories.subscription_lifecycle_repo
app.services.subscription_lifecycle_query_service
app.schemas.subscription_lifecycle

Result: passed
```

## Notes

One parallel test run failed because the lifecycle migration suite and member/v2 suite were run at the same time against the same `gymflow_test` database. The suites mutate and reset shared database state, so they must run sequentially. Rerunning sequentially passed.

The repository currently applies some derived-state filters after paging. This is acceptable for Phase 3 read-foundation work because no API cutover was performed. A future API-facing route can promote those filters into SQL/window queries when pagination semantics become a public contract.

## Phase 3 Verdict

```text
Subscription Lifecycle Phase 3 read layer: implemented
Routes changed:                             no
Frontend changed:                           no
Write mutations added:                      no
Payments added:                             no
Legacy subscription models changed:         no
Tests:                                      passed
Ready for review/commit:                    yes
Safe to begin Phase 4 after review:         yes
```
