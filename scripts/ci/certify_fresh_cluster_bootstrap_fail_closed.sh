#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SQL_FILE="$(mktemp)"
trap 'rm -f "$SQL_FILE"' EXIT
cd "$ROOT"
python -s scripts/render_cluster_role_bootstrap.py > "$SQL_FILE"

expect_bootstrap_failure() {
  local label="$1"
  shift
  set +e
  "$@"
  local status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "P2B BLOCK: bootstrap unexpectedly succeeded for $label" >&2
    exit 1
  fi
}

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2b_wrong_bootstrap_admin SUPERUSER NOLOGIN;
SQL

set +e
{
  printf '%s\n' 'SET ROLE p2b_wrong_bootstrap_admin;'
  cat "$SQL_FILE"
} | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres
wrong_admin_status=$?
set -e
if [[ "$wrong_admin_status" -eq 0 ]]; then
  echo 'P2B BLOCK: bootstrap accepted a non-canonical administrative identity' >&2
  exit 1
fi
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c 'DROP ROLE p2b_wrong_bootstrap_admin;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE app_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION BYPASSRLS;
SQL

expect_bootstrap_failure "pre-existing unsafe managed role" \
  sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -f "$SQL_FILE"

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

echo 'Fresh bootstrap negative paths proved fail-closed without drift repair'
