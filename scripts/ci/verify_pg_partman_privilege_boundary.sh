#!/usr/bin/env bash
set -euo pipefail

_pg_topology_env=()
if [[ "${PGHOST+x}" == "x" ]]; then
  if [[ -z "$PGHOST" ]]; then
    echo 'PGHOST was explicitly supplied but empty' >&2
    exit 2
  fi
  _pg_topology_env+=("PGHOST=$PGHOST")
fi
if [[ "${PGPORT+x}" == "x" ]]; then
  if [[ ! "$PGPORT" =~ ^[0-9]+$ ]] || (( PGPORT < 1 || PGPORT > 65535 )); then
    echo "PGPORT was explicitly supplied but is not a valid TCP port: $PGPORT" >&2
    exit 2
  fi
  _pg_topology_env+=("PGPORT=$PGPORT")
fi

_postgres_psql() {
  local database="$1"
  if [[ -z "$database" ]]; then
    echo 'database argument must not be empty' >&2
    exit 2
  fi
  if [[ "${#_pg_topology_env[@]}" -gt 0 ]]; then
    sudo -u postgres env "${_pg_topology_env[@]}" psql -X -v ON_ERROR_STOP=1 -d "$database"
  else
    sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$database"
  fi
}

if [[ "$#" -ne 1 ]]; then
  echo 'usage: verify_pg_partman_privilege_boundary.sh DATABASE' >&2
  exit 2
fi

database="$1"

_postgres_psql "$database" <<'SQL'
DO $$
DECLARE
  unexpected record;
  signature text;
  routine_oid oid;
  allowed_signatures text[] := ARRAY[
    'partman.apply_constraints(text,text,boolean,bigint)',
    'partman.apply_privileges(text,text,text,text,bigint)',
    'partman.calculate_time_partition_info(interval,timestamp with time zone,text)',
    'partman.check_automatic_maintenance_value(text)',
    'partman.check_control_type(text,text,text)',
    'partman.check_default(boolean)',
    'partman.check_epoch_type(text)',
    'partman.check_name_length(text,text,boolean)',
    'partman.check_partition_type(text)',
    'partman.check_subpart_sameconfig(text)',
    'partman.check_subpartition_limits(text,text)',
    'partman.create_parent(text,text,text,text,text,integer,text,boolean,text,text[],text,boolean,text)',
    'partman.create_partition_id(text,bigint[],text)',
    'partman.create_partition_time(text,timestamp with time zone[],text)',
    'partman.drop_partition_id(text,bigint,boolean,boolean,text)',
    'partman.drop_partition_time(text,interval,boolean,boolean,text,timestamp with time zone)',
    'partman.inherit_template_properties(text,text,text)',
    'partman.run_maintenance(text,boolean,boolean)',
    'partman.show_partition_info(text,text,text)',
    'partman.show_partition_name(text,text)',
    'partman.show_partitions(text,text,boolean)'
  ];
BEGIN
  FOREACH signature IN ARRAY allowed_signatures LOOP
    SELECT pg_catalog.to_regprocedure(signature) INTO routine_oid;
    IF routine_oid IS NULL THEN
      RAISE EXCEPTION 'missing required pg_partman routine signature %', signature;
    END IF;
    IF NOT pg_catalog.has_function_privilege('migration_owner', routine_oid, 'EXECUTE') THEN
      RAISE EXCEPTION 'migration_owner lacks EXECUTE on %', signature;
    END IF;
  END LOOP;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_extension e
    JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
    JOIN pg_catalog.pg_roles r ON r.oid = e.extowner
    WHERE e.extname = 'pg_partman'
      AND e.extversion = '5.0.1'
      AND n.nspname = 'partman'
      AND r.rolname <> 'migration_owner'
  ) THEN
    RAISE EXCEPTION 'pg_partman extension version/schema/owner boundary drifted';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'migration_owner'
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'migration_owner role attributes are over-privileged';
  END IF;

  IF NOT pg_catalog.has_schema_privilege('migration_owner', 'partman', 'USAGE') THEN
    RAISE EXCEPTION 'migration_owner lacks USAGE on partman';
  END IF;
  IF pg_catalog.has_schema_privilege('migration_owner', 'partman', 'CREATE') THEN
    RAISE EXCEPTION 'migration_owner unexpectedly has CREATE on partman';
  END IF;

  IF NOT (
    pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'SELECT') AND
    pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'INSERT') AND
    pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'UPDATE') AND
    pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'DELETE')
  ) THEN
    RAISE EXCEPTION 'migration_owner lacks required part_config DML';
  END IF;

  IF pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'TRUNCATE') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'REFERENCES') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config', 'TRIGGER') THEN
    RAISE EXCEPTION 'migration_owner has unexpected broad part_config privilege';
  END IF;

  IF NOT pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'SELECT') THEN
    RAISE EXCEPTION 'migration_owner lacks required part_config_sub SELECT';
  END IF;
  IF pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'INSERT') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'UPDATE') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'DELETE') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'TRUNCATE') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'REFERENCES') OR
     pg_catalog.has_table_privilege('migration_owner', 'partman.part_config_sub', 'TRIGGER') THEN
    RAISE EXCEPTION 'migration_owner has unexpected broad part_config_sub privilege';
  END IF;

  FOR unexpected IN
    SELECT n.nspname, c.relname, privilege
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(privilege)
    WHERE n.nspname = 'partman'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND c.relname NOT IN ('part_config', 'part_config_sub')
      AND pg_catalog.has_table_privilege('migration_owner', c.oid, p.privilege)
  LOOP
    RAISE EXCEPTION 'unexpected migration_owner table privilege %.% %', unexpected.nspname, unexpected.relname, unexpected.privilege;
  END LOOP;

  FOR unexpected IN
    SELECT n.nspname, c.relname, privilege
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
    WHERE n.nspname = 'partman'
      AND c.relkind = 'S'
      AND pg_catalog.has_sequence_privilege('migration_owner', c.oid, p.privilege)
  LOOP
    RAISE EXCEPTION 'unexpected migration_owner sequence privilege %.% %', unexpected.nspname, unexpected.relname, unexpected.privilege;
  END LOOP;

  FOR unexpected IN
    SELECT p.oid::regprocedure::text AS routine
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'partman'
      AND p.prokind IN ('f', 'p')
      AND NOT p.oid = ANY (ARRAY(SELECT pg_catalog.to_regprocedure(allowed_signature) FROM unnest(allowed_signatures) AS allowed_signature))
      AND pg_catalog.has_function_privilege('migration_owner', p.oid, 'EXECUTE')
  LOOP
    RAISE EXCEPTION 'unexpected migration_owner routine EXECUTE %', unexpected.routine;
  END LOOP;
END $$;

SELECT 'pg_partman privilege boundary verified' AS status;
SQL
