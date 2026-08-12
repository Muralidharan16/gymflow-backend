#!/usr/bin/env bash
set -euo pipefail

# Production/pre-production cluster bootstrap for the cross-tenant lifecycle
# maintenance capability. This script intentionally does not use sudo, does not
# contain credentials, and does not grant the capability to any login. Connect
# with a separately controlled cluster-administrator identity via normal libpq
# variables (PGHOST/PGPORT/PGUSER/PGPASSWORD/PGSSLMODE, etc.).
#
# Optional:
#   DOERS_CLUSTER_ADMIN_DATABASE=postgres
#
# The script is idempotent only for an already-safe role. It refuses to mutate
# an existing role that has unsafe attributes or forbidden capability edges.

PSQL_BIN="${PSQL_BIN:-psql}"
ADMIN_DATABASE="${DOERS_CLUSTER_ADMIN_DATABASE:-postgres}"

if ! command -v "$PSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: psql client not found: $PSQL_BIN" >&2
  exit 2
fi

"$PSQL_BIN" -X -v ON_ERROR_STOP=1 --dbname="$ADMIN_DATABASE" <<'SQL'
DO $$
DECLARE
  operator_record record;
  target_record record;
  required_role text;
BEGIN
  SELECT * INTO operator_record
  FROM pg_catalog.pg_roles
  WHERE rolname = current_user;

  IF operator_record IS NULL
     OR NOT (operator_record.rolsuper OR operator_record.rolcreaterole) THEN
    RAISE EXCEPTION
      'cluster bootstrap requires a dedicated administrator with SUPERUSER or CREATEROLE; current_user=%',
      current_user;
  END IF;

  FOREACH required_role IN ARRAY ARRAY[
    'migration_owner',
    'app_runtime',
    'auth_runtime',
    'worker_runtime',
    'app_security_owner',
    'app_rls_executor'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = required_role
    ) THEN
      RAISE EXCEPTION
        'required cluster role % must be provisioned before lifecycle maintenance capability',
        required_role;
    END IF;
  END LOOP;

  SELECT * INTO target_record
  FROM pg_catalog.pg_roles
  WHERE rolname = 'lifecycle_maintenance_runtime';

  IF FOUND AND (
       target_record.rolcanlogin
       OR target_record.rolsuper
       OR target_record.rolcreatedb
       OR target_record.rolcreaterole
       OR target_record.rolinherit
       OR target_record.rolreplication
       OR target_record.rolbypassrls
     ) THEN
    RAISE EXCEPTION
      'existing lifecycle_maintenance_runtime has unsafe attributes; refuse automatic repair';
  END IF;
END
$$;

SELECT
  'CREATE ROLE lifecycle_maintenance_runtime '
  'NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT '
  'NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = 'lifecycle_maintenance_runtime'
)\gexec

ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '15s';
ALTER ROLE lifecycle_maintenance_runtime SET lock_timeout = '2s';
ALTER ROLE lifecycle_maintenance_runtime SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE lifecycle_maintenance_runtime SET row_security = 'on';

DO $$
DECLARE
  target_record record;
  required_setting text;
  forbidden_role text;
BEGIN
  SELECT * INTO target_record
  FROM pg_catalog.pg_roles
  WHERE rolname = 'lifecycle_maintenance_runtime';

  IF NOT FOUND
     OR target_record.rolcanlogin
     OR target_record.rolsuper
     OR target_record.rolcreatedb
     OR target_record.rolcreaterole
     OR target_record.rolinherit
     OR target_record.rolreplication
     OR target_record.rolbypassrls THEN
    RAISE EXCEPTION
      'lifecycle_maintenance_runtime violates reduced NOLOGIN/NOINHERIT/NOBYPASSRLS contract';
  END IF;

  FOREACH required_setting IN ARRAY ARRAY[
    'statement_timeout=15s',
    'lock_timeout=2s',
    'idle_in_transaction_session_timeout=30s',
    'row_security=on'
  ]
  LOOP
    IF NOT (COALESCE(target_record.rolconfig, ARRAY[]::text[]) @> ARRAY[required_setting]) THEN
      RAISE EXCEPTION
        'lifecycle_maintenance_runtime missing required role setting %; observed=%',
        required_setting,
        target_record.rolconfig;
    END IF;
  END LOOP;

  IF pg_catalog.pg_has_role(
       'migration_owner',
       'lifecycle_maintenance_runtime',
       'MEMBER'
     )
     OR pg_catalog.pg_has_role(
       'migration_owner',
       'lifecycle_maintenance_runtime',
       'SET'
     ) THEN
    RAISE EXCEPTION
      'migration_owner must not access lifecycle_maintenance_runtime';
  END IF;

  FOREACH forbidden_role IN ARRAY ARRAY[
    'app_runtime',
    'auth_runtime',
    'worker_runtime',
    'app_security_owner',
    'app_rls_executor'
  ]
  LOOP
    IF pg_catalog.pg_has_role(
         'lifecycle_maintenance_runtime',
         forbidden_role,
         'MEMBER'
       )
       OR pg_catalog.pg_has_role(
         'lifecycle_maintenance_runtime',
         forbidden_role,
         'SET'
       ) THEN
      RAISE EXCEPTION
        'lifecycle_maintenance_runtime must not inherit or SET ROLE to %',
        forbidden_role;
    END IF;
  END LOOP;
END
$$;
SQL

echo "lifecycle_maintenance_runtime production capability contract verified"
