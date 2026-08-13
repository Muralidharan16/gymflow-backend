#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be the API runtime URL}"
: "${AUTH_DATABASE_URL:?AUTH_DATABASE_URL must be set}"
: "${WORKER_DATABASE_URL:?WORKER_DATABASE_URL must be set}"
: "${MAINTENANCE_DATABASE_URL:?MAINTENANCE_DATABASE_URL must be set}"

expect_failure() {
  local label="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    echo "FAIL: ${label} unexpectedly passed" >&2
    echo "${output}" >&2
    exit 1
  fi
  echo "PASS: ${label} rejected"
}

python -s scripts/verify_runtime_principal_bindings.py

expect_failure \
  "worker URL swapped to API login" \
  env WORKER_DATABASE_URL="$DATABASE_URL" \
  python -s scripts/verify_runtime_principal_bindings.py

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime BYPASSRLS;
SQL
expect_failure \
  "worker deployment login BYPASSRLS drift" \
  python -s scripts/verify_runtime_principal_bindings.py
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE worker_test_runtime NOBYPASSRLS;
SQL
python -s scripts/verify_runtime_principal_bindings.py

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT app_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
SQL
expect_failure \
  "worker gains API capability" \
  python -s scripts/verify_runtime_principal_bindings.py
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
REVOKE app_runtime FROM worker_test_runtime;
SQL
python -s scripts/verify_runtime_principal_bindings.py

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT worker_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;
SQL
expect_failure \
  "worker can SET ROLE to worker capability" \
  python -s scripts/verify_runtime_principal_bindings.py
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT worker_runtime TO worker_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
SQL
python -s scripts/verify_runtime_principal_bindings.py

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE auth_test_runtime SET row_security = 'off';
SQL
expect_failure \
  "auth deployment login row_security disabled" \
  python -s scripts/verify_runtime_principal_bindings.py
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
ALTER ROLE auth_test_runtime SET row_security = 'on';
SQL
python -s scripts/verify_runtime_principal_bindings.py

echo "P2D runtime principal negative-path certification passed"
