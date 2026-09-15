#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

: "${MIGRATION_PASSWORD:=ci-p7o-migration-owner}"
: "${APP_RUNTIME_PASSWORD:=ci-p7o-app-runtime}"
: "${AUTH_RUNTIME_PASSWORD:=ci-p7o-auth-runtime}"
: "${P7O_SLOW_STARTED_FILE:=/tmp/p7o-slow-started}"
: "${P7O_SLOW_SECONDS:=20}"

LOG=/tmp/p7o-api.log
CERT_DIR=/tmp/p7o-redis-tls
CONFIG_DIR=/tmp/p7o-redis-config
API_PID=''
SLOW_PID=''
PRESTOP_PID=''

cleanup() {
  set +e
  if [ -n "${API_PID}" ] && kill -0 "${API_PID}" 2>/dev/null; then
    kill -TERM "${API_PID}"
    wait "${API_PID}"
  fi
  if [ -n "${SLOW_PID}" ] && kill -0 "${SLOW_PID}" 2>/dev/null; then
    kill "${SLOW_PID}"
    wait "${SLOW_PID}"
  fi
  if [ -n "${PRESTOP_PID}" ] && kill -0 "${PRESTOP_PID}" 2>/dev/null; then
    kill "${PRESTOP_PID}"
    wait "${PRESTOP_PID}"
  fi
  docker rm -f p7o-replica p7o-primary >/dev/null 2>&1 || true
  docker network rm p7o-redis-net >/dev/null 2>&1 || true
  docker volume rm p7o-replica-data p7o-primary-data >/dev/null 2>&1 || true
  set -e
}
trap cleanup EXIT

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '${MIGRATION_PASSWORD}';
CREATE ROLE app_p7o_runtime LOGIN PASSWORD '${APP_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT app_runtime TO app_p7o_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO app_p7o_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE app_p7o_runtime SET row_security='on';
ALTER ROLE app_p7o_runtime SET statement_timeout='5s';
ALTER ROLE app_p7o_runtime SET lock_timeout='500ms';
ALTER ROLE app_p7o_runtime SET idle_in_transaction_session_timeout='15s';

CREATE ROLE auth_p7o_runtime LOGIN PASSWORD '${AUTH_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT auth_runtime TO auth_p7o_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO auth_p7o_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE auth_p7o_runtime SET row_security='on';
ALTER ROLE auth_p7o_runtime SET statement_timeout='5s';
ALTER ROLE auth_p7o_runtime SET lock_timeout='500ms';
ALTER ROLE auth_p7o_runtime SET idle_in_transaction_session_timeout='15s';

CREATE DATABASE gymflow_p7o_test OWNER migration_owner;
REVOKE ALL ON DATABASE gymflow_p7o_test FROM PUBLIC;
GRANT CONNECT ON DATABASE gymflow_p7o_test TO migration_owner, app_p7o_runtime, auth_p7o_runtime;
SQL
bash scripts/ci/verify_cluster_roles.sh
bash scripts/ci/provision_infrastructure_extensions.sh gymflow_p7o_test

export DATABASE_URL="postgresql+asyncpg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/gymflow_p7o_test"
python -s scripts/verify_alembic_graph.py
python -s -m alembic -c alembic.ini upgrade head
python -s -m alembic -c alembic.ini current --check-heads

sudo apt-get update
sudo apt-get install -y redis-tools
sudo sysctl -w vm.overcommit_memory=1
test "$(cat /proc/sys/vm/overcommit_memory)" = "1"
rm -rf "${CERT_DIR}" "${CONFIG_DIR}"
mkdir -p "${CERT_DIR}" "${CONFIG_DIR}"

openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
  -keyout "${CERT_DIR}/ca.key" -out "${CERT_DIR}/ca.crt" \
  -subj '/CN=DOERS P7-O CI CA'
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "${CERT_DIR}/server.key" -out "${CERT_DIR}/server.csr" \
  -subj '/CN=p7o-primary'
cat > "${CERT_DIR}/server.ext" <<'EOF_EXT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,DNS:p7o-primary,DNS:p7o-replica,IP:127.0.0.1
EOF_EXT
openssl x509 -req -sha256 -days 1 \
  -in "${CERT_DIR}/server.csr" -CA "${CERT_DIR}/ca.crt" \
  -CAkey "${CERT_DIR}/ca.key" -CAcreateserial \
  -out "${CERT_DIR}/server.crt" -extfile "${CERT_DIR}/server.ext"
chmod 0644 "${CERT_DIR}/ca.crt" "${CERT_DIR}/server.crt" "${CERT_DIR}/server.key"

P7O_REDIS_PASSWORD="$(openssl rand -hex 24)"
export P7O_REDIS_PASSWORD P7O_CERT_DIR="${CERT_DIR}"
echo "::add-mask::${P7O_REDIS_PASSWORD}"

cat > "${CONFIG_DIR}/primary.conf" <<EOF_PRIMARY
port 0
tls-port 6379
tls-cert-file /tls/server.crt
tls-key-file /tls/server.key
tls-ca-cert-file /tls/ca.crt
tls-auth-clients no
requirepass ${P7O_REDIS_PASSWORD}
dir /data
appendonly yes
appendfsync everysec
save 60 1
maxmemory 128mb
maxmemory-policy noeviction
EOF_PRIMARY

cat > "${CONFIG_DIR}/replica.conf" <<EOF_REPLICA
port 0
tls-port 6379
tls-cert-file /tls/server.crt
tls-key-file /tls/server.key
tls-ca-cert-file /tls/ca.crt
tls-auth-clients no
requirepass ${P7O_REDIS_PASSWORD}
masterauth ${P7O_REDIS_PASSWORD}
replicaof p7o-primary 6379
tls-replication yes
dir /data
appendonly yes
appendfsync everysec
save 60 1
maxmemory 128mb
maxmemory-policy noeviction
EOF_REPLICA

docker network create p7o-redis-net
docker volume create p7o-primary-data
docker volume create p7o-replica-data
docker run -d --name p7o-primary --network p7o-redis-net -p 16379:6379 \
  -v p7o-primary-data:/data -v "${CERT_DIR}:/tls:ro" \
  -v "${CONFIG_DIR}:/config:ro" redis:7-alpine redis-server /config/primary.conf
docker run -d --name p7o-replica --network p7o-redis-net \
  -v p7o-replica-data:/data -v "${CERT_DIR}:/tls:ro" \
  -v "${CONFIG_DIR}:/config:ro" redis:7-alpine redis-server /config/replica.conf

for attempt in $(seq 1 60); do
  if REDISCLI_AUTH="${P7O_REDIS_PASSWORD}" redis-cli --tls --cacert "${CERT_DIR}/ca.crt" \
      -h localhost -p 16379 ping 2>/dev/null | grep -qx PONG; then
    break
  fi
  if [ "${attempt}" -eq 60 ]; then
    docker logs p7o-primary
    exit 1
  fi
  sleep 1
done
for attempt in $(seq 1 60); do
  connected="$(REDISCLI_AUTH="${P7O_REDIS_PASSWORD}" redis-cli --tls --cacert "${CERT_DIR}/ca.crt" \
    -h localhost -p 16379 INFO replication | tr -d '\r' | awk -F: '/^connected_slaves:/{print $2}')"
  if [ "${connected}" = "1" ]; then
    break
  fi
  if [ "${attempt}" -eq 60 ]; then
    docker logs p7o-replica
    exit 1
  fi
  sleep 1
done

REDISS_BASE="rediss://:${P7O_REDIS_PASSWORD}@localhost:16379"
REDISS_QUERY="ssl_cert_reqs=required&ssl_ca_certs=${CERT_DIR}/ca.crt"
export REDIS_URL="${REDISS_BASE}/0?${REDISS_QUERY}"
export CELERY_BROKER_URL="${REDISS_BASE}/1?${REDISS_QUERY}"
export CELERY_RESULT_BACKEND="${REDISS_BASE}/2?${REDISS_QUERY}"
export REDIS_PRODUCTION_TOPOLOGY=self_managed_ha
export REDIS_PERSISTENCE_MODE=aof_everysec_rdb
export REDIS_MAXMEMORY_POLICY=noeviction
export REDIS_HA_MIN_REPLICAS=1
export REDIS_VM_OVERCOMMIT_MEMORY=1
export REDIS_MANAGED_PROVIDER_ATTESTED=false

python -s scripts/verify_redis_production_readiness.py \
  --host localhost \
  --port 16379 \
  --ca-cert "${CERT_DIR}/ca.crt" \
  --password-env P7O_REDIS_PASSWORD \
  --expected-replicas 1 \
  --require-local-overcommit

export ENVIRONMENT=production
export DOERS_PROCESS_PROFILE=api
export CELERY_WORKER_PROFILE=''
export DATABASE_URL="postgresql+asyncpg://app_p7o_runtime:${APP_RUNTIME_PASSWORD}@127.0.0.1:5432/gymflow_p7o_test"
export AUTH_DATABASE_URL="postgresql+asyncpg://auth_p7o_runtime:${AUTH_RUNTIME_PASSWORD}@127.0.0.1:5432/gymflow_p7o_test"
export WORKER_DATABASE_URL=''
export MAINTENANCE_DATABASE_URL=''
export FINANCE_CONFIG_DATABASE_URL=''
export NOTIFICATION_EMAIL_PROVIDER_MODE=disabled
export SEARCH_PROVIDER_MODE=disabled
export P4E_METRICS_OTLP_ENDPOINT=''
export SECRET_KEY="${SECRET_KEY:-p7o-ci-not-production-secret}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-p7o-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-p7o-test}"
export AWS_REGION_NAME="${AWS_REGION_NAME:-us-east-1}"
export S3_BUCKET_NAME="${S3_BUCKET_NAME:-gymflow-p7o-test}"
export P7O_SLOW_STARTED_FILE P7O_SLOW_SECONDS

rm -f "${P7O_SLOW_STARTED_FILE}" /tmp/p7o-slow.json /tmp/p7o-prestop.json "${LOG}"
DOERS_PRESTOP_CONTROL_TOKEN="$(openssl rand -hex 32)"
export DOERS_PRESTOP_CONTROL_TOKEN
echo "::add-mask::${DOERS_PRESTOP_CONTROL_TOKEN}"

redis_normal_count() {
  REDISCLI_AUTH="${P7O_REDIS_PASSWORD}" redis-cli --tls --cacert "${CERT_DIR}/ca.crt" \
    -h localhost -p 16379 --no-auth-warning CLIENT LIST TYPE normal | sed '/^$/d' | wc -l
}
redis_baseline="$(redis_normal_count)"

uvicorn scripts.ci.p7o_runtime_app:app --host 127.0.0.1 --port 8765 > "${LOG}" 2>&1 &
API_PID=$!

for attempt in $(seq 1 90); do
  live_code="$(curl -sS -o /tmp/p7o-live.json -w '%{http_code}' http://127.0.0.1:8765/_system/live 2>/dev/null || printf '000')"
  ready_code="$(curl -sS -o /tmp/p7o-ready.json -w '%{http_code}' http://127.0.0.1:8765/_system/ready 2>/dev/null || printf '000')"
  if [ "${live_code}" = "200" ] && [ "${ready_code}" = "200" ]; then
    break
  fi
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    cat "${LOG}"
    exit 1
  fi
  if [ "${attempt}" -eq 90 ]; then
    cat "${LOG}"
    exit 1
  fi
  sleep 1
done

missing_code="$(curl -sS -o /tmp/p7o-missing.json -w '%{http_code}' -X POST \
  http://127.0.0.1:8765/_system/preStop)"
if [ "${missing_code}" != "403" ]; then
  echo 'missing preStop token unexpectedly authorized' >&2
  cat /tmp/p7o-missing.json
  exit 1
fi
test "$(curl -sS -o /tmp/p7o-ready-after-missing.json -w '%{http_code}' \
  http://127.0.0.1:8765/_system/ready)" = "200"

wrong_code="$(curl -sS -o /tmp/p7o-wrong.json -w '%{http_code}' -X POST \
  -H 'X-Doers-PreStop-Token: definitely-wrong-token-value' \
  http://127.0.0.1:8765/_system/preStop)"
if [ "${wrong_code}" != "403" ]; then
  echo 'wrong preStop token unexpectedly authorized' >&2
  cat /tmp/p7o-wrong.json
  exit 1
fi
test "$(curl -sS -o /tmp/p7o-ready-after-wrong.json -w '%{http_code}' \
  http://127.0.0.1:8765/_system/ready)" = "200"

curl -fsS http://127.0.0.1:8765/_p7/slow > /tmp/p7o-slow.json &
SLOW_PID=$!
for attempt in $(seq 1 30); do
  if [ -s "${P7O_SLOW_STARTED_FILE}" ]; then
    break
  fi
  if [ "${attempt}" -eq 30 ]; then
    cat "${LOG}"
    exit 1
  fi
  sleep 1
done

db_live_count="$(PGPASSWORD="${MIGRATION_PASSWORD}" psql -X -At \
  -h 127.0.0.1 -U migration_owner -d gymflow_p7o_test \
  -c "SELECT count(*) FROM pg_stat_activity WHERE usename='app_p7o_runtime';")"
test "${db_live_count}" -ge 1
redis_live="$(redis_normal_count)"
test "${redis_live}" -gt "${redis_baseline}"

curl -fsS -X POST -H "X-Doers-PreStop-Token: ${DOERS_PRESTOP_CONTROL_TOKEN}" \
  http://127.0.0.1:8765/_system/preStop > /tmp/p7o-prestop.json &
PRESTOP_PID=$!

drained=0
for attempt in $(seq 1 30); do
  code="$(curl -sS -o /tmp/p7o-ready-draining.json -w '%{http_code}' \
    http://127.0.0.1:8765/_system/ready)"
  if [ "${code}" = "503" ]; then
    drained=1
    break
  fi
  sleep 1
done
if [ "${drained}" -ne 1 ]; then
  echo 'readiness did not become 503 during drain' >&2
  cat "${LOG}"
  exit 1
fi

if ! kill -0 "${PRESTOP_PID}" 2>/dev/null; then
  echo 'preStop returned before admitted slow request completed' >&2
  exit 1
fi
if [ "$(curl -sS -o /tmp/p7o-live-draining.json -w '%{http_code}' \
    http://127.0.0.1:8765/_system/live)" != "200" ]; then
  echo 'liveness failed while draining' >&2
  exit 1
fi

new_code="$(curl -sS -o /tmp/p7o-new-during-drain.json -w '%{http_code}' \
  http://127.0.0.1:8765/)"
if [ "${new_code}" != "503" ]; then
  echo 'new request was not rejected during drain' >&2
  cat /tmp/p7o-new-during-drain.json
  exit 1
fi
python - <<'PY'
import json
payload = json.load(open('/tmp/p7o-new-during-drain.json', encoding='utf-8'))
assert payload['reason'] == 'pod_draining', payload
PY

set +e
wait "${SLOW_PID}"
slow_rc=$?
set -e
SLOW_PID=''
if [ "${slow_rc}" -ne 0 ]; then
  echo 'slow admitted request did not complete' >&2
  cat "${LOG}"
  exit 1
fi
python - <<'PY'
import json
payload = json.load(open('/tmp/p7o-slow.json', encoding='utf-8'))
assert payload['status'] == 'completed', payload
assert payload['db_roundtrip'] is True, payload
PY

set +e
wait "${PRESTOP_PID}"
prestop_rc=$?
set -e
PRESTOP_PID=''
test "${prestop_rc}" -eq 0
python - <<'PY'
import json
payload = json.load(open('/tmp/p7o-prestop.json', encoding='utf-8'))
assert payload['status'] == 'drained', payload
PY
test "$(curl -sS -o /tmp/p7o-live-before-term.json -w '%{http_code}' \
  http://127.0.0.1:8765/_system/live)" = "200"

kill -TERM "${API_PID}"
set +e
wait "${API_PID}"
api_rc=$?
set -e
API_PID=''
if [ "${api_rc}" -ne 0 ]; then
  cat "${LOG}"
  exit "${api_rc}"
fi

db_after="$(PGPASSWORD="${MIGRATION_PASSWORD}" psql -X -At \
  -h 127.0.0.1 -U migration_owner -d gymflow_p7o_test \
  -c "SELECT count(*) FROM pg_stat_activity WHERE usename IN ('app_p7o_runtime','auth_p7o_runtime');")"
if [ "${db_after}" != "0" ]; then
  echo 'API database connections remain after shutdown' >&2
  exit 1
fi
redis_after="$(redis_normal_count)"
if [ "${redis_after}" != "${redis_baseline}" ]; then
  echo 'Redis client count did not return to baseline' >&2
  echo "baseline=${redis_baseline} after=${redis_after}" >&2
  exit 1
fi

grep -q 'Application shutdown complete' "${LOG}"
echo 'P7O_REAL_UVICORN_RUNTIME=PASS'
echo 'P7O_POSTGRES_RESOURCE_CLEANUP=PASS'
echo 'P7O_REDIS_RESOURCE_CLEANUP=PASS'
