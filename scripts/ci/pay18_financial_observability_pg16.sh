#!/usr/bin/env bash
set -euo pipefail
umask 077

DB_NAME="gymflow_pay18_test"
MIGRATION_PASSWORD="ci-pay18-migration"
MAINT_PASSWORD="ci-pay18-maintenance"
MAINT_LOGIN="pay18_maintenance_test"
PREDECESSOR="zz27d8e9f0a62"
HEAD="zz37d8e9f0a63"
SNAPSHOT="app_secure.pay18_financial_observability_snapshot()"

echo "======================================================================"
echo " DOERS — PAY-18 PG16 FINANCIAL OBSERVABILITY PROOF"
echo " ROUNDTRIP / REDUCED EXECUTE / NO DIRECT SELECT / AGGREGATE-ONLY SHAPE"
echo "======================================================================"

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '${MIGRATION_PASSWORD}';
CREATE ROLE ${MAINT_LOGIN} LOGIN PASSWORD '${MAINT_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT lifecycle_maintenance_runtime TO ${MAINT_LOGIN}
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE ${MAINT_LOGIN} SET row_security='on';
ALTER ROLE ${MAINT_LOGIN} SET statement_timeout='5s';
ALTER ROLE ${MAINT_LOGIN} SET lock_timeout='2s';
CREATE DATABASE ${DB_NAME} OWNER migration_owner;
REVOKE ALL ON DATABASE ${DB_NAME} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${DB_NAME} TO migration_owner,${MAINT_LOGIN};
SQL

bash scripts/ci/provision_infrastructure_extensions.sh "${DB_NAME}"

export DATABASE_URL="postgresql+asyncpg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

python -s -m alembic -c alembic.ini upgrade "${PREDECESSOR}"
python -s -m alembic -c alembic.ini upgrade "${HEAD}"
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${HEAD}"

python -s -m alembic -c alembic.ini downgrade "${PREDECESSOR}"
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${PREDECESSOR}"

python -s -m alembic -c alembic.ini upgrade "${HEAD}"
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${HEAD}"
echo 'PAY18_EMPTY_MIGRATION_ROUNDTRIP=PASS'

function_access="$(
  PGPASSWORD="${MAINT_PASSWORD}" psql -X -qAt -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -U "${MAINT_LOGIN}" -d "${DB_NAME}" \
    -c "SELECT pg_catalog.has_function_privilege(
          current_user,
          'app_secure.pay18_financial_observability_snapshot()',
          'EXECUTE'
        )"
)"
test "${function_access}" = "t"

for relation in \
  finance.payment_events \
  finance.payment_application_records \
  finance.outbox_events \
  finance.provider_webhook_inbox \
  public.platform_payment_attempts \
  public.platform_refunds \
  public.platform_mandates \
  public.platform_dunning_cases \
  public.platform_disputes \
  public.platform_accounting_reconciliation_items
do
  schema_name="${relation%%.*}"
  table_name="${relation#*.}"
  direct="$(
    sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "${DB_NAME}" \
      -v role_name="${MAINT_LOGIN}" \
      -v schema_name="${schema_name}" \
      -v table_name="${table_name}" \
      -c "SELECT pg_catalog.has_table_privilege(
            :'role_name',
            relation_data.oid,
            'SELECT'
          )
          FROM pg_catalog.pg_class AS relation_data
          JOIN pg_catalog.pg_namespace AS namespace_data
            ON namespace_data.oid=relation_data.relnamespace
          WHERE namespace_data.nspname=:'schema_name'
            AND relation_data.relname=:'table_name'"
  )"
  test "${direct}" = "f"
done
echo 'PAY18_MAINTENANCE_DIRECT_FINANCE_SELECT=0'

expected_columns="$(
  cat <<'EOF' | LC_ALL=C sort
chargeback_open_total
dunning_billing_only_total
dunning_full_grace_total
dunning_limited_write_total
dunning_read_only_total
dunning_recovered_total
duplicate_payment_allegation_open_total
finance_outbox_backlog
mandate_expired_total
mandate_failed_total
mandate_revoked_total
payment_application_backlog
payment_attempt_total
payment_failure_total
payment_unknown_total
platform_refund_backlog
platform_refund_unknown_total
reconciliation_open_total
settlement_mismatch_total
webhook_backlog
EOF
)"

actual_columns="$(
  PGPASSWORD="${MAINT_PASSWORD}" psql -X -qAt -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -U "${MAINT_LOGIN}" -d "${DB_NAME}" <<SQL | LC_ALL=C sort
SELECT pg_catalog.jsonb_object_keys(pg_catalog.to_jsonb(snapshot_row))
FROM ${SNAPSHOT} AS snapshot_row;
SQL
)"
test "${actual_columns}" = "${expected_columns}"

values="$(
  PGPASSWORD="${MAINT_PASSWORD}" psql -X -qAt -F '|' -v ON_ERROR_STOP=1 \
    -h 127.0.0.1 -U "${MAINT_LOGIN}" -d "${DB_NAME}" <<SQL
SELECT
  payment_attempt_total,
  payment_failure_total,
  payment_unknown_total,
  webhook_backlog,
  payment_application_backlog,
  finance_outbox_backlog,
  platform_refund_backlog,
  platform_refund_unknown_total,
  settlement_mismatch_total,
  reconciliation_open_total,
  mandate_failed_total,
  mandate_expired_total,
  mandate_revoked_total,
  dunning_full_grace_total,
  dunning_limited_write_total,
  dunning_read_only_total,
  dunning_billing_only_total,
  dunning_recovered_total,
  chargeback_open_total,
  duplicate_payment_allegation_open_total
FROM ${SNAPSHOT};
SQL
)"
test "${values}" = "0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0"

for forbidden_role in app_runtime worker_runtime; do
  access="$(
    sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "${DB_NAME}" \
      -c "SELECT pg_catalog.has_function_privilege(
            '${forbidden_role}',
            'app_secure.pay18_financial_observability_snapshot()',
            'EXECUTE'
          )"
  )"
  test "${access}" = "f"
done

echo 'PAY18_AGGREGATE_ONLY_SNAPSHOT=PASS'
echo 'PAY18_MAINTENANCE_EXECUTE_ONLY=PASS'
echo 'PAY18_RUNTIME_OBSERVABILITY_AUTHORITY=PASS'
