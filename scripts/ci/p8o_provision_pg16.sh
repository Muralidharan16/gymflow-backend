#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:?usage: p8o_provision_pg16.sh operational|p5d|p5w2}"

: "${MIGRATION_PASSWORD:?MIGRATION_PASSWORD is required}"
: "${AUTH_RUNTIME_PASSWORD:?AUTH_RUNTIME_PASSWORD is required}"
: "${APP_RUNTIME_PASSWORD:?APP_RUNTIME_PASSWORD is required}"
: "${WORKER_RUNTIME_PASSWORD:?WORKER_RUNTIME_PASSWORD is required}"

case "${PROFILE}" in
  operational)
    DB_NAME="gymflow_p8o_test"
    AUTH_LOGIN="auth_p4e_runtime"
    : "${MAINTENANCE_RUNTIME_PASSWORD:?MAINTENANCE_RUNTIME_PASSWORD is required}"
    ;;
  p5d)
    DB_NAME="gymflow_p5d_test"
    AUTH_LOGIN="auth_p5d_runtime"
    ;;
  p5w2)
    DB_NAME="gymflow_p5w2_test"
    AUTH_LOGIN="auth_p5w2_runtime"
    ;;
  *)
    echo "unsupported P8-O profile: ${PROFILE}" >&2
    exit 2
    ;;
esac

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -v migration_password="${MIGRATION_PASSWORD}" \
  -v auth_password="${AUTH_RUNTIME_PASSWORD}" \
  -v app_password="${APP_RUNTIME_PASSWORD}" \
  -v worker_password="${WORKER_RUNTIME_PASSWORD}" \
  -v maintenance_password="${MAINTENANCE_RUNTIME_PASSWORD:-unused}" \
  -v auth_login="${AUTH_LOGIN}" \
  -v db_name="${DB_NAME}" <<'SQL'
ALTER ROLE migration_owner PASSWORD :'migration_password';

SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'auth_login', :'auth_password'
) \gexec
CREATE ROLE app_test_runtime LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE worker_test_runtime LOGIN PASSWORD :'worker_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

SELECT format('GRANT auth_runtime TO %I WITH ADMIN FALSE, INHERIT TRUE, SET FALSE', :'auth_login') \gexec
SELECT format('GRANT app_user TO %I WITH ADMIN FALSE, INHERIT TRUE, SET FALSE', :'auth_login') \gexec
GRANT app_runtime TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT worker_runtime TO worker_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;

SELECT format('ALTER ROLE %I SET row_security=%L', :'auth_login', 'on') \gexec
SELECT format('ALTER ROLE %I SET statement_timeout=%L', :'auth_login', '5s') \gexec
SELECT format('ALTER ROLE %I SET lock_timeout=%L', :'auth_login', '2s') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout=%L', :'auth_login', '30s') \gexec
ALTER ROLE app_test_runtime SET row_security='on';
ALTER ROLE app_test_runtime SET statement_timeout='10s';
ALTER ROLE app_test_runtime SET lock_timeout='2s';
ALTER ROLE app_test_runtime SET idle_in_transaction_session_timeout='30s';
ALTER ROLE worker_test_runtime SET row_security='on';
ALTER ROLE worker_test_runtime SET statement_timeout='15s';
ALTER ROLE worker_test_runtime SET lock_timeout='2s';
ALTER ROLE worker_test_runtime SET idle_in_transaction_session_timeout='30s';
SQL

if [[ "${PROFILE}" == "operational" ]]; then
  sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
    -v maintenance_password="${MAINTENANCE_RUNTIME_PASSWORD}" <<'SQL'
CREATE ROLE lifecycle_maintenance_test_runtime LOGIN PASSWORD :'maintenance_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT lifecycle_maintenance_runtime TO lifecycle_maintenance_test_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE lifecycle_maintenance_test_runtime SET row_security='on';
ALTER ROLE lifecycle_maintenance_test_runtime SET statement_timeout='15s';
ALTER ROLE lifecycle_maintenance_test_runtime SET lock_timeout='2s';
ALTER ROLE lifecycle_maintenance_test_runtime SET idle_in_transaction_session_timeout='30s';
SQL
fi

sudo -u postgres createdb -O migration_owner "${DB_NAME}"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  -v db_name="${DB_NAME}" -v auth_login="${AUTH_LOGIN}" <<'SQL'
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db_name') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO migration_owner, %I, app_test_runtime, worker_test_runtime', :'db_name', :'auth_login') \gexec
SQL

if [[ "${PROFILE}" == "operational" ]]; then
  sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -v db_name="${DB_NAME}" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO lifecycle_maintenance_test_runtime', :'db_name') \gexec
SQL
fi

bash scripts/ci/verify_cluster_roles.sh
bash scripts/ci/provision_infrastructure_extensions.sh "${DB_NAME}"
