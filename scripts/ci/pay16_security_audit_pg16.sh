#!/usr/bin/env bash
set -euo pipefail
umask 077

DB_NAME="gymflow_pay16_test"
MIGRATION_PASSWORD="ci-pay16-migration"
APP_PASSWORD="ci-pay16-api"
APP_LOGIN="pay16_api_runtime"
PREDECESSOR="zz17d8e9f0a61"
HEAD="zz27d8e9f0a62"
ORG_ID="99000000-0000-0000-0000-000000000001"
ACTOR_ID="99000000-0000-0000-0000-000000000101"

echo "======================================================================"
echo " DOERS — PAY-16 PG16 SECURITY AUDIT PROOF"
echo " EMPTY ROUNDTRIP / LEAST PRIVILEGE / HASH CHAIN / FAIL-CLOSED DOWNGRADE"
echo "======================================================================"

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '${MIGRATION_PASSWORD}';
CREATE ROLE ${APP_LOGIN} LOGIN PASSWORD '${APP_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT app_runtime TO ${APP_LOGIN}
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE ${APP_LOGIN} SET row_security='on';
ALTER ROLE ${APP_LOGIN} SET statement_timeout='5s';
ALTER ROLE ${APP_LOGIN} SET lock_timeout='2s';
CREATE DATABASE ${DB_NAME} OWNER migration_owner;
REVOKE ALL ON DATABASE ${DB_NAME} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${DB_NAME} TO migration_owner,${APP_LOGIN};
SQL

bash scripts/ci/provision_infrastructure_extensions.sh "${DB_NAME}"

export DATABASE_URL="postgresql+asyncpg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

python -s -m alembic -c alembic.ini upgrade "${PREDECESSOR}"
python -s -m alembic -c alembic.ini upgrade "${HEAD}"
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${HEAD}"

python -s -m alembic -c alembic.ini downgrade "${PREDECESSOR}"
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${PREDECESSOR}"

python -s -m alembic -c alembic.ini upgrade "${HEAD}"
echo 'PAY16_EMPTY_MIGRATION_ROUNDTRIP=PASS'

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "${DB_NAME}" <<SQL
INSERT INTO public.organizations(
    id,name,slug,tier,is_active,max_branches,default_currency_code
) VALUES (
    '${ORG_ID}',
    'PAY16 Security Org','pay16-security-org','basic',true,5,'INR'
);
SQL

for privilege in SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER; do
  value="$(
    PGPASSWORD="${APP_PASSWORD}" psql -X -qAt -v ON_ERROR_STOP=1       -h 127.0.0.1 -U "${APP_LOGIN}" -d "${DB_NAME}"       -c "SELECT pg_catalog.has_table_privilege(current_user,'finance.security_audit_events','${privilege}')"
  )"
  test "${value}" = "f"
done

function_access="$(
  PGPASSWORD="${APP_PASSWORD}" psql -X -qAt -v ON_ERROR_STOP=1     -h 127.0.0.1 -U "${APP_LOGIN}" -d "${DB_NAME}"     -c "SELECT pg_catalog.has_function_privilege(current_user,'app_secure.record_finance_security_audit(text,text,uuid,text,text)','EXECUTE')"
)"
test "${function_access}" = "t"

PGPASSWORD="${APP_PASSWORD}" psql -X -v ON_ERROR_STOP=1   -h 127.0.0.1 -U "${APP_LOGIN}" -d "${DB_NAME}" <<SQL
BEGIN;
SELECT pg_catalog.set_config('app.current_org_id','${ORG_ID}',true);
SELECT pg_catalog.set_config('app.current_user_id','${ACTOR_ID}',true);
SELECT pg_catalog.set_config('app.current_role','owner',true);
SELECT pg_catalog.set_config('app.request_id','pay16-proof-1',true);
SELECT app_secure.record_finance_security_audit(
    'finance.security.checkout.initiated',
    'payment',
    '99000000-0000-0000-0000-000000000201',
    'CHECKOUT_INITIATED',
    'info'
);
SELECT pg_catalog.set_config('app.request_id','pay16-proof-2',true);
SELECT app_secure.record_finance_security_audit(
    'finance.security.offline_payment.approved',
    'offline_payment',
    '99000000-0000-0000-0000-000000000301',
    'MANUAL_BANK_PROOF',
    'info'
);
COMMIT;
SQL

read -r first_sequence first_prev first_hash second_sequence second_prev second_hash < <(
  sudo -u postgres psql -X -qAt -F ' ' -v ON_ERROR_STOP=1     -d "${DB_NAME}" <<SQL
WITH ordered AS (
  SELECT
    sequence_no,
    COALESCE(previous_event_hash,'GENESIS') AS previous_event_hash,
    event_hash,
    row_number() OVER (ORDER BY sequence_no) AS rn
  FROM finance.security_audit_events
  WHERE organization_id='${ORG_ID}'
)
SELECT
  max(sequence_no) FILTER (WHERE rn=1),
  max(previous_event_hash) FILTER (WHERE rn=1),
  max(event_hash) FILTER (WHERE rn=1),
  max(sequence_no) FILTER (WHERE rn=2),
  max(previous_event_hash) FILTER (WHERE rn=2),
  max(event_hash) FILTER (WHERE rn=2)
FROM ordered;
SQL
)

test "${first_sequence}" = "1"
test "${first_prev}" = "GENESIS"
test "${second_sequence}" = "2"
test -n "${first_hash}"
test -n "${second_hash}"
test "${first_hash}" = "${second_prev}"

if sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "${DB_NAME}"   -c "UPDATE finance.security_audit_events SET severity='warning'"; then
  echo 'PAY-16 immutable UPDATE unexpectedly succeeded' >&2
  exit 1
fi

if sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "${DB_NAME}"   -c "TRUNCATE finance.security_audit_events"; then
  echo 'PAY-16 immutable TRUNCATE unexpectedly succeeded' >&2
  exit 1
fi

if python -s -m alembic -c alembic.ini downgrade "${PREDECESSOR}"; then
  echo 'PAY-16 populated downgrade unexpectedly succeeded' >&2
  exit 1
fi
test "$(python -s -m alembic -c alembic.ini current | awk '{print $1}')" = "${HEAD}"

echo 'PAY16_AUDIT_CHAIN=PASS'
echo 'PAY16_AUDIT_IMMUTABLE=PASS'
echo 'PAY16_AUDIT_LEAST_PRIVILEGE=PASS'
echo 'PAY16_POPULATED_DOWNGRADE=FAIL_CLOSED'
