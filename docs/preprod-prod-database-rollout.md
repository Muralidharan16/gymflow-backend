# Pre-Production and Production Database Rollout Notes

This project is still pre-production. The P0B fix intentionally repaired historical
migrations so a fresh database can be built reliably from revision zero to head.
After a migration has been deployed to customer environments, do not edit that
historical migration. Add a new corrective migration instead.

## Current Local Status

- Pytest must use `TEST_DATABASE_URL`.
- Pytest must never point at the development or production app database.
- The current local `gymflow` development database has lost `organizations` and
  `owners` rows and should be rebuilt or restored before browser verification.
- `gymflow_test` is disposable and may be dropped/recreated for test runs.

## Pre-Production Checklist

1. Create a fresh pre-production database from scratch.
2. Run `alembic upgrade head`.
3. Confirm `alembic_version` is at the latest head.
4. Confirm `v_active_org_branches` is a view, not a table.
5. Confirm `transactional_outbox` has `uq_outbox_dedupe`.
6. Confirm `branch_hours_audit_log` has both `old_data` and `new_data`.
7. Run backend regression tests with a separate pre-production test database.
8. Sign up a fresh organization through the application flow.
9. Complete onboarding.
10. Create a branch.
11. Create a member.
12. Create a membership plan.
13. Run admission/subscription browser verification.
14. Do not begin member payments until admission/subscription verification passes.

## Production Checklist

1. Do not edit historical migrations that have already reached production.
2. For production schema fixes, add a new forward-only corrective migration.
3. Take a verified backup before running migrations.
4. Run migrations first in a production-like clone.
5. Record the current `alembic_version` before deployment.
6. Run `alembic upgrade head` during the approved deployment window.
7. Verify schema invariants after migration:
   - `v_active_org_branches` is a view.
   - `transactional_outbox.uq_outbox_dedupe` exists.
   - `branch_hours_audit_log.old_data` exists.
   - `branch_hours_audit_log.new_data` exists.
8. Run smoke checks for signup, login, onboarding, branch creation, member
   creation, membership plan creation, and admission/subscription creation.
9. Monitor application errors and database locks after deployment.
10. Keep rollback instructions and the backup restore path ready.

## Test Database Rules

- Required variable:
  `TEST_DATABASE_URL=postgresql+asyncpg://.../<database_name_containing_test>`
- Never set `TEST_DATABASE_URL` to `gymflow`, pre-production, or production.
- Safe cleanup may truncate only guarded test databases.
- Do not use `session_replication_role = 'replica'` in tests.
- Do not hard-code destructive cleanup against business tables such as
  `organizations` or `owners`.

## Development Database Recovery

For local development, choose one path:

1. Restore `gymflow` from a known-good backup.
2. Intentionally reset local `gymflow`, run `alembic upgrade head`, and recreate
   data through the normal application flow.

After local recovery:

1. Start backend.
2. Sign up a fresh organization.
3. Complete onboarding.
4. Create a branch.
5. Create a member.
6. Create a membership plan.
7. Resume A5 admission browser verification.
8. Start member payments only after A5 passes.
