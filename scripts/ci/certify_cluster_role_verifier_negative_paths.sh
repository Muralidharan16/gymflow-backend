#!/usr/bin/env bash
set -euo pipefail

: "${DOERS_CLUSTER_VERIFY_DATABASE_URL:?DOERS_CLUSTER_VERIFY_DATABASE_URL is required}"
: "${P2B_DATABASE:?P2B_DATABASE is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

verify_ok() {
  python -s scripts/verify_cluster_role_bootstrap.py
}

expect_verify_failure() {
  local label="$1"
  set +e
  output="$(python -s scripts/verify_cluster_role_bootstrap.py 2>&1)"
  status=$?
  set -e
  printf '%s\n' "$output"
  if [[ "$status" -eq 0 ]]; then
    echo "P2B BLOCK: read-only verifier accepted $label" >&2
    exit 1
  fi
}

verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE worker_runtime BYPASSRLS;'
expect_verify_failure 'worker_runtime BYPASSRLS drift'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE worker_runtime NOBYPASSRLS;'
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE lifecycle_maintenance_runtime LOGIN;'
expect_verify_failure 'maintenance LOGIN drift'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE lifecycle_maintenance_runtime NOLOGIN;'
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE app_runtime INHERIT;'
expect_verify_failure 'app_runtime INHERIT drift'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'ALTER ROLE app_runtime NOINHERIT;'
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  "ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '16s';"
expect_verify_failure 'maintenance timeout drift'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  "ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '15s';"
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT app_runtime TO migration_owner WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SQL
expect_verify_failure 'extra migration-owner runtime membership'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'REVOKE app_runtime FROM migration_owner;'
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
REVOKE app_rls_executor FROM migration_owner;
GRANT app_rls_executor TO migration_owner WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;
SQL
expect_verify_failure 'wrong migration-owner membership options'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
REVOKE app_rls_executor FROM migration_owner;
GRANT app_rls_executor TO migration_owner WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SQL
verify_ok

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE app_runtime IN DATABASE "$P2B_DATABASE" SET statement_timeout = '1s';
SQL
expect_verify_failure 'database-specific managed-role setting override'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE app_runtime IN DATABASE "$P2B_DATABASE" RESET statement_timeout;
SQL
verify_ok

echo 'Live read-only verifier negative paths passed and canonical state was explicitly restored'
