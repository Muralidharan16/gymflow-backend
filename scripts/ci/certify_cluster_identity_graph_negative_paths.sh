#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

verify_fails_with() {
  local label="$1"
  local expected="$2"
  local output status

  set +e
  output="$(python -s scripts/verify_cluster_identity_graph.py 2>&1)"
  status=$?
  set -e

  printf '%s\n' "$output"
  if [[ "$status" -eq 0 ]]; then
    echo "P2C BLOCK: verifier unexpectedly accepted $label" >&2
    exit 1
  fi
  if ! grep -Fq -- "$expected" <<<"$output"; then
    echo "P2C BLOCK: $label failed for an unrelated reason; expected: $expected" >&2
    exit 1
  fi
}

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT auth_runtime TO app_runtime
  WITH ADMIN FALSE, INHERIT FALSE, SET FALSE;
SQL
verify_fails_with \
  'direct MEMBER-only API to auth edge' \
  '[identity.member_reachability] app_runtime->auth_runtime.MEMBER'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'REVOKE auth_runtime FROM app_runtime;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2c_set_bridge NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT p2c_set_bridge TO worker_runtime
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT auth_runtime TO p2c_set_bridge
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SQL
verify_fails_with \
  'transitive worker SET bridge to auth' \
  '[identity.set_reachability] worker_runtime->auth_runtime.SET'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'DROP ROLE p2c_set_bridge;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2c_usage_bridge NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT p2c_usage_bridge TO app_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT worker_runtime TO p2c_usage_bridge
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
SQL
verify_fails_with \
  'transitive API USAGE bridge to worker' \
  '[identity.usage_reachability] app_runtime->worker_runtime.USAGE'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'DROP ROLE p2c_usage_bridge;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
GRANT worker_runtime TO app_security_owner
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SQL
verify_fails_with \
  'migration helper onward bridge' \
  '[identity.set_reachability] app_security_owner->worker_runtime.SET'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'REVOKE worker_runtime FROM app_security_owner;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2c_admin_bridge NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT p2c_admin_bridge TO app_runtime
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT auth_runtime TO p2c_admin_bridge
  WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
SQL
verify_fails_with \
  'SET-reachable ADMIN escalation bridge' \
  '[identity.admin_escalation] app_runtime->auth_runtime'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'DROP ROLE p2c_admin_bridge;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE p2c_unapproved_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;
GRANT app_rls_executor TO p2c_unapproved_login
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
SQL
verify_fails_with \
  'unapproved direct migration-helper delegation' \
  '[identity.non_delegable_grant] p2c_unapproved_login->app_rls_executor'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'DROP ROLE p2c_unapproved_login;'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'ALTER ROLE worker_runtime CREATEROLE;'
verify_fails_with \
  'protected CREATEROLE mutation authority' \
  '[identity.graph_mutation_attribute] worker_runtime.create_role'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -c 'ALTER ROLE worker_runtime NOCREATEROLE;'

python -s scripts/verify_cluster_identity_graph.py
echo 'P2C adversarial identity graph matrix rejected exact escalation paths and restored canonical state'
