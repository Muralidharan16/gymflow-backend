#!/usr/bin/env bash
set -euo pipefail

: "${MIGRATION_PASSWORD:?MIGRATION_PASSWORD is required}"
: "${GENERAL_RUNTIME_PASSWORD:?GENERAL_RUNTIME_PASSWORD is required}"
: "${AUTH_RUNTIME_PASSWORD:?AUTH_RUNTIME_PASSWORD is required}"
: "${WORKER_RUNTIME_PASSWORD:?WORKER_RUNTIME_PASSWORD is required}"
: "${MAINTENANCE_RUNTIME_PASSWORD:?MAINTENANCE_RUNTIME_PASSWORD is required}"

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '${MIGRATION_PASSWORD}';

CREATE ROLE test_runner LOGIN PASSWORD 'ci-test-runner'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE app_test_runtime LOGIN PASSWORD '${GENERAL_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE auth_test_runtime LOGIN PASSWORD '${AUTH_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE worker_test_runtime LOGIN PASSWORD '${WORKER_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE lifecycle_maintenance_test_runtime LOGIN PASSWORD '${MAINTENANCE_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

ALTER ROLE app_test_runtime SET row_security='on';
ALTER ROLE app_test_runtime SET statement_timeout='5s';
ALTER ROLE app_test_runtime SET lock_timeout='2s';
ALTER ROLE app_test_runtime SET idle_in_transaction_session_timeout='15s';
ALTER ROLE auth_test_runtime SET row_security='on';
ALTER ROLE auth_test_runtime SET statement_timeout='5s';
ALTER ROLE auth_test_runtime SET lock_timeout='2s';
ALTER ROLE auth_test_runtime SET idle_in_transaction_session_timeout='15s';
ALTER ROLE worker_test_runtime SET row_security='on';
ALTER ROLE worker_test_runtime SET statement_timeout='15s';
ALTER ROLE worker_test_runtime SET lock_timeout='2s';
ALTER ROLE worker_test_runtime SET idle_in_transaction_session_timeout='30s';
ALTER ROLE lifecycle_maintenance_test_runtime SET row_security='on';
ALTER ROLE lifecycle_maintenance_test_runtime SET statement_timeout='15s';
ALTER ROLE lifecycle_maintenance_test_runtime SET lock_timeout='2s';
ALTER ROLE lifecycle_maintenance_test_runtime SET idle_in_transaction_session_timeout='30s';

GRANT app_runtime TO app_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO app_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT auth_runtime TO auth_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO auth_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT worker_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT lifecycle_maintenance_runtime TO lifecycle_maintenance_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;

CREATE DATABASE gymflow_migration_test OWNER migration_owner;
CREATE DATABASE gymflow_test OWNER migration_owner;
GRANT CONNECT ON DATABASE gymflow_test
  TO app_test_runtime, auth_test_runtime, worker_test_runtime, lifecycle_maintenance_test_runtime;
SQL

bash scripts/ci/verify_cluster_roles.sh
bash scripts/ci/provision_infrastructure_extensions.sh gymflow_migration_test
bash scripts/ci/provision_infrastructure_extensions.sh gymflow_test

python -s scripts/verify_alembic_graph.py
python -s scripts/migration_semantics_gate.py
test "$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')" = 'q07d8e9f0a31'
python -s -m alembic -c alembic.ini upgrade head
python -s -m alembic -c alembic.ini current --check-heads
DATABASE_URL="postgresql+asyncpg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/gymflow_test" \
  python -s -m alembic -c alembic.ini upgrade head
DATABASE_URL="postgresql+asyncpg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/gymflow_test" \
  python -s -m alembic -c alembic.ini current --check-heads
