#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  echo 'usage: provision_infrastructure_extensions.sh DATABASE [DATABASE...]' >&2
  exit 2
fi

for database in "$@"; do
  sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$database" <<'SQL'
CREATE SCHEMA IF NOT EXISTS partman AUTHORIZATION postgres;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS postgis;
GRANT USAGE ON SCHEMA partman TO migration_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA partman TO migration_owner;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA partman TO migration_owner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO migration_owner;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO migration_owner;
SQL
done
