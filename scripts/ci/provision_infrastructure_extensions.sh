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

if [[ "$#" -eq 0 ]]; then
  echo 'usage: provision_infrastructure_extensions.sh DATABASE [DATABASE...]' >&2
  exit 2
fi

for database in "$@"; do
  _postgres_psql "$database" <<'SQL'
CREATE SCHEMA IF NOT EXISTS partman AUTHORIZATION postgres;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS postgis;
REVOKE CREATE ON SCHEMA partman FROM migration_owner;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA partman FROM migration_owner;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA partman FROM migration_owner;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA partman FROM migration_owner;
REVOKE ALL PRIVILEGES ON ALL PROCEDURES IN SCHEMA partman FROM migration_owner;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA partman FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL PROCEDURES IN SCHEMA partman FROM PUBLIC;

GRANT USAGE ON SCHEMA partman TO migration_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE partman.part_config TO migration_owner;
GRANT SELECT ON TABLE partman.part_config_sub TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.apply_constraints(text,text,boolean,bigint) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.apply_privileges(text,text,text,text,bigint) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.calculate_time_partition_info(interval,timestamp with time zone,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_automatic_maintenance_value(text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_control_type(text,text,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_default(boolean) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_epoch_type(text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_name_length(text,text,boolean) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_partition_type(text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_subpart_sameconfig(text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.check_subpartition_limits(text,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.create_parent(text,text,text,text,text,integer,text,boolean,text,text[],text,boolean,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.create_partition_id(text,bigint[],text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.create_partition_time(text,timestamp with time zone[],text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.drop_partition_id(text,bigint,boolean,boolean,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.drop_partition_time(text,interval,boolean,boolean,text,timestamp with time zone) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.inherit_template_properties(text,text,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.run_maintenance(text,boolean,boolean) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.show_partition_info(text,text,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.show_partition_name(text,text) TO migration_owner;
GRANT EXECUTE ON FUNCTION partman.show_partitions(text,text,boolean) TO migration_owner;
SQL
done
