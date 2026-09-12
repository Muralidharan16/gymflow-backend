# Test Database Safety

Pytest must never run against the development database. The app runtime still uses
`DATABASE_URL`, but pytest requires `TEST_DATABASE_URL` and refuses unsafe names.

## Required Environment

Use a separate PostgreSQL database whose name clearly contains `test`. Credentials
must come from the local environment or secret manager; never commit them to the
repository:

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://test_runner:<password-from-secret-store>@localhost:5432/gymflow_test'
```

Pytest fails fast when:

- `TEST_DATABASE_URL` is missing.
- `TEST_DATABASE_URL` points to the same database as `DATABASE_URL`.
- The test database name does not contain `test`.

## Create And Migrate The Test Database

Create the database through the approved database bootstrap/admin path, then run
Alembic with the reduced migration identity:

```bash
DATABASE_URL='postgresql+asyncpg://migration_owner:<password-from-secret-store>@localhost:5432/gymflow_test' \
PYTHONPATH=. .venv/bin/alembic upgrade head
```

Then run tests with the dedicated test identity:

```bash
TEST_DATABASE_URL='postgresql+asyncpg://test_runner:<password-from-secret-store>@localhost:5432/gymflow_test' \
PYTHONPATH=. .venv/bin/pytest -q tests/test_auth_register.py
```

## Cleanup Rules

Test cleanup is guarded by `assert_test_database()`, which checks
`SELECT current_database()` before truncating any tables. Cleanup refuses to run
unless the active database name contains `test`.

Tests must not use:

```sql
SET session_replication_role = 'replica'
```

That disables FK constraints and can leave orphaned records.

## Current Corrupted Development Database

Do not partially repair the development DB automatically. Safe options:

- Restore from a known-good backup.
- Accept a clean development reset and recreate seed data.
- Add a separate guarded reset script only if it requires explicit confirmation,
  prints the target database name, and refuses to run unless
  `ALLOW_DESTRUCTIVE_DB_RESET=true` is set.

Never run destructive cleanup against `gymflow` from tests.
