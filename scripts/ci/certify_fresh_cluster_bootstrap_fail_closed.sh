#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

assert_expected_failure() {
  local label="$1"
  local expected="$2"
  local status="$3"
  local output="$4"

  printf '%s\n' "$output"
  if [[ "$status" -eq 0 ]]; then
    echo "P2B BLOCK: bootstrap unexpectedly succeeded for $label" >&2
    exit 1
  fi
  if ! grep -Fq -- "$expected" <<<"$output"; then
    echo "P2B BLOCK: $label failed for an unrelated reason; expected: $expected" >&2
    exit 1
  fi
}

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2b_wrong_bootstrap_admin SUPERUSER NOLOGIN;
SQL

set +e
wrong_admin_output="$({
  printf '%s\n' 'SET ROLE p2b_wrong_bootstrap_admin;'
  python -s scripts/render_cluster_role_bootstrap.py
} | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres 2>&1)"
wrong_admin_status=$?
set -e
assert_expected_failure \
  'non-canonical administrative identity' \
  'fresh cluster bootstrap requires current_user=session_user=postgres with SUPERUSER' \
  "$wrong_admin_status" \
  "$wrong_admin_output"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c \
  'DROP ROLE p2b_wrong_bootstrap_admin;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE app_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION BYPASSRLS;
SQL

set +e
existing_role_output="$(
  python -s scripts/render_cluster_role_bootstrap.py \
    | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres 2>&1
)"
existing_role_status=$?
set -e
assert_expected_failure \
  'pre-existing unsafe managed role' \
  'fresh cluster bootstrap refuses existing managed/retired roles: app_runtime' \
  "$existing_role_status" \
  "$existing_role_output"

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'app_runtime'
      AND rolbypassrls
      AND NOT rolcanlogin
  ) THEN
    RAISE EXCEPTION 'fresh bootstrap silently repaired or replaced unsafe app_runtime';
  END IF;
END
$$;
DROP ROLE app_runtime;
SQL

echo 'Fresh bootstrap negative paths proved exact fail-closed behavior without drift repair'
