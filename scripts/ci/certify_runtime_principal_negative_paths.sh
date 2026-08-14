#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be the API runtime URL}"
: "${AUTH_DATABASE_URL:?AUTH_DATABASE_URL must be set}"
: "${WORKER_DATABASE_URL:?WORKER_DATABASE_URL must be set}"
: "${MAINTENANCE_DATABASE_URL:?MAINTENANCE_DATABASE_URL must be set}"

expect_failure() {
  local label="$1"
  local expected="$2"
  shift 2
  local output
  if output="$("$@" 2>&1)"; then
    echo "FAIL: ${label} unexpectedly passed" >&2
    echo "${output}" >&2
    exit 1
  fi
  if [[ "${output}" != *"${expected}"* ]]; then
    echo "FAIL: ${label} failed for an unrelated reason" >&2
    echo "Expected diagnostic: ${expected}" >&2
    echo "${output}" >&2
    exit 1
  fi
  echo "PASS: ${label} rejected with ${expected}"
}

verify() {
  python -s scripts/verify_runtime_principal_bindings.py
}

verify

expect_failure \
  "worker URL swapped to API login" \
  "runtime.direct_membership_set" \
  env WORKER_DATABASE_URL="$DATABASE_URL" \
  python -s scripts/verify_runtime_principal_bindings.py

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime BYPASSRLS;
SQL
expect_failure \
  "worker deployment login BYPASSRLS drift" \
  "runtime.dangerous_login_attribute" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime NOBYPASSRLS;
SQL
verify

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT app_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
SQL
expect_failure \
  "worker gains API capability" \
  "runtime.direct_membership_set" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
REVOKE app_runtime FROM worker_test_runtime;
SQL
verify

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT worker_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;
SQL
expect_failure \
  "worker can SET ROLE to worker capability" \
  "runtime.membership_option" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT worker_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
SQL
verify

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE auth_test_runtime SET row_security = 'off';
SQL
expect_failure \
  "auth deployment login row_security disabled" \
  "runtime.row_security_disabled" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE auth_test_runtime SET row_security = 'on';
SQL
verify

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime SET statement_timeout = '45s';
SQL
expect_failure \
  "worker deployment login timeout drift" \
  "runtime.role_setting" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime SET statement_timeout = '15s';
SQL
verify

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime IN DATABASE gymflow_p2d SET lock_timeout = '9s';
SQL
expect_failure \
  "worker database-specific role setting override" \
  "runtime.database_specific_setting" \
  verify
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime IN DATABASE gymflow_p2d RESET lock_timeout;
SQL
verify

echo "P2D runtime principal negative-path certification passed"
