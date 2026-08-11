#!/usr/bin/env bash
set -euo pipefail

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'lifecycle_maintenance_runtime'
  ) THEN
    RAISE EXCEPTION 'lifecycle_maintenance_runtime already exists before CI bootstrap';
  END IF;
END
$$;

CREATE ROLE lifecycle_maintenance_runtime
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  NOREPLICATION NOBYPASSRLS;

ALTER ROLE lifecycle_maintenance_runtime SET statement_timeout = '15s';
ALTER ROLE lifecycle_maintenance_runtime SET lock_timeout = '2s';
ALTER ROLE lifecycle_maintenance_runtime SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE lifecycle_maintenance_runtime SET row_security = 'on';

DO $$
DECLARE
  role_record record;
BEGIN
  SELECT * INTO role_record
  FROM pg_catalog.pg_roles
  WHERE rolname = 'lifecycle_maintenance_runtime';

  IF role_record.rolcanlogin
     OR role_record.rolsuper
     OR role_record.rolcreatedb
     OR role_record.rolcreaterole
     OR role_record.rolinherit
     OR role_record.rolreplication
     OR role_record.rolbypassrls THEN
    RAISE EXCEPTION 'lifecycle_maintenance_runtime role attributes are unsafe';
  END IF;

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
    RAISE EXCEPTION 'migration_owner may access lifecycle maintenance capability';
  END IF;
END
$$;
SQL
