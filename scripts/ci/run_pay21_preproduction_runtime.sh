#!/usr/bin/env bash
set -euo pipefail
umask 077
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

: "${PAY21_CANDIDATE_SHA:?}"
: "${PAY21_ALEMBIC_HEAD:?}"
: "${PAY21_RUNTIME_EVIDENCE:?}"

DB="gymflow_pay21_preprod_test"
NET="pay21-preprod-net"
IMAGE="doers-pay21:${PAY21_CANDIDATE_SHA}"
TLS_DIR="/tmp/pay21-tls"
NGINX_DIR="/tmp/pay21-nginx"
REDIS_DIR="/tmp/pay21-redis"
SOCAT_PID=""
OTLP_PID=""

cleanup() {
  set +e
  docker rm -f pay21-ingress pay21-bad-api pay21-api pay21-worker pay21-redis >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  if [ -n "$OTLP_PID" ]; then
    kill "$OTLP_PID" >/dev/null 2>&1 || true
    wait "$OTLP_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$SOCAT_PID" ]; then
    kill "$SOCAT_PID" >/dev/null 2>&1 || true
    wait "$SOCAT_PID" >/dev/null 2>&1 || true
  fi
  set -e
}
trap cleanup EXIT

mkdir -p "$(dirname "$PAY21_RUNTIME_EVIDENCE")"
rm -rf "$TLS_DIR" "$NGINX_DIR" "$REDIS_DIR"
mkdir -p "$TLS_DIR" "$NGINX_DIR" "$REDIS_DIR"

bash scripts/ci/install_pg16_test_stack.sh
bash scripts/ci/bootstrap_cluster_roles.sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends socat redis-tools

MIGRATION_PASSWORD="$(openssl rand -hex 24)"
API_PASSWORD="$(openssl rand -hex 24)"
AUTH_PASSWORD="$(openssl rand -hex 24)"
WORKER_PASSWORD="$(openssl rand -hex 24)"
MAINT_PASSWORD="$(openssl rand -hex 24)"
CONFIG_PASSWORD="$(openssl rand -hex 24)"
REDIS_PASSWORD="$(openssl rand -hex 24)"
APP_SECRET="$(openssl rand -hex 32)"
PRESTOP_TOKEN="$(openssl rand -hex 32)"
for secret in "$MIGRATION_PASSWORD" "$API_PASSWORD" "$AUTH_PASSWORD" "$WORKER_PASSWORD" "$MAINT_PASSWORD" "$CONFIG_PASSWORD" "$REDIS_PASSWORD" "$APP_SECRET" "$PRESTOP_TOKEN"; do
  echo "::add-mask::$secret"
done

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '$MIGRATION_PASSWORD';

CREATE ROLE pay21_api_runtime LOGIN PASSWORD '$API_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT app_runtime TO pay21_api_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO pay21_api_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE pay21_api_runtime SET row_security='on';
ALTER ROLE pay21_api_runtime SET statement_timeout='5s';
ALTER ROLE pay21_api_runtime SET lock_timeout='2s';
ALTER ROLE pay21_api_runtime SET idle_in_transaction_session_timeout='15s';

CREATE ROLE pay21_auth_runtime LOGIN PASSWORD '$AUTH_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT auth_runtime TO pay21_auth_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO pay21_auth_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE pay21_auth_runtime SET row_security='on';
ALTER ROLE pay21_auth_runtime SET statement_timeout='5s';
ALTER ROLE pay21_auth_runtime SET lock_timeout='2s';
ALTER ROLE pay21_auth_runtime SET idle_in_transaction_session_timeout='15s';

CREATE ROLE pay21_worker_runtime LOGIN PASSWORD '$WORKER_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT worker_runtime TO pay21_worker_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE pay21_worker_runtime SET row_security='on';
ALTER ROLE pay21_worker_runtime SET statement_timeout='15s';
ALTER ROLE pay21_worker_runtime SET lock_timeout='2s';
ALTER ROLE pay21_worker_runtime SET idle_in_transaction_session_timeout='30s';

CREATE ROLE pay21_maintenance_runtime LOGIN PASSWORD '$MAINT_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT lifecycle_maintenance_runtime TO pay21_maintenance_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE pay21_maintenance_runtime SET row_security='on';
ALTER ROLE pay21_maintenance_runtime SET statement_timeout='15s';
ALTER ROLE pay21_maintenance_runtime SET lock_timeout='2s';
ALTER ROLE pay21_maintenance_runtime SET idle_in_transaction_session_timeout='30s';

CREATE ROLE pay21_finance_config_runtime LOGIN PASSWORD '$CONFIG_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT finance_config_runtime TO pay21_finance_config_runtime
  WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE pay21_finance_config_runtime SET row_security='on';
ALTER ROLE pay21_finance_config_runtime SET statement_timeout='15s';
ALTER ROLE pay21_finance_config_runtime SET lock_timeout='2s';
ALTER ROLE pay21_finance_config_runtime SET idle_in_transaction_session_timeout='30s';

DROP DATABASE IF EXISTS $DB WITH (FORCE);
CREATE DATABASE $DB OWNER migration_owner;
REVOKE ALL ON DATABASE $DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $DB TO
  pay21_api_runtime,pay21_auth_runtime,pay21_worker_runtime,
  pay21_maintenance_runtime,pay21_finance_config_runtime;
SQL

export MIGRATION_PASSWORD
bash scripts/ci/verify_cluster_roles.sh
bash scripts/ci/provision_infrastructure_extensions.sh "$DB"

export DATABASE_URL="postgresql+asyncpg://migration_owner:$MIGRATION_PASSWORD@127.0.0.1:5432/$DB"
python -s scripts/verify_alembic_graph.py
test "$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')" = "$PAY21_ALEMBIC_HEAD"
python -s -m alembic -c alembic.ini upgrade head
python -s -m alembic -c alembic.ini current --check-heads

# Runtime logins must remain reduced and mutually separated.
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$DB" <<'SQL'
DO $$
DECLARE
  role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY[
    'pay21_api_runtime','pay21_auth_runtime','pay21_worker_runtime',
    'pay21_maintenance_runtime','pay21_finance_config_runtime'
  ]
  LOOP
    IF EXISTS (
      SELECT 1 FROM pg_catalog.pg_roles
      WHERE rolname=role_name
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
      RAISE EXCEPTION 'PAY-21 privileged runtime role: %', role_name;
    END IF;
  END LOOP;
END $$;
SQL

docker network create "$NET" >/dev/null
GATEWAY="$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Gateway}}')"
test -n "$GATEWAY"

# Expose host PostgreSQL only onto the private Docker bridge, not a public runner port.
socat TCP-LISTEN:55432,reuseaddr,fork,bind="$GATEWAY" TCP:127.0.0.1:5432 >/tmp/pay21-socat.log 2>&1 &
SOCAT_PID=$!
sleep 1
kill -0 "$SOCAT_PID"

# Production profiles fail closed without an OTLP metrics sink. Bind the
# disposable collector only to the private Docker bridge gateway so API/worker
# telemetry is real without publishing another runner/customer-facing port.
OTLP_CAPTURE="/tmp/pay21-otlp.jsonl"
python scripts/ci/p8o_otlp_collector.py \
  --host "$GATEWAY" --port 4318 --capture "$OTLP_CAPTURE" \
  >/tmp/pay21-otlp.log 2>&1 &
OTLP_PID=$!
for attempt in $(seq 1 30); do
  if curl -fsS "http://$GATEWAY:4318/healthz" >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    cat /tmp/pay21-otlp.log
    exit 1
  fi
  sleep 1
done
OTLP_ENDPOINT="http://$GATEWAY:4318/v1/metrics"

openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes   -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt"   -subj '/CN=DOERS PAY21 Preproduction CA'
openssl req -newkey rsa:2048 -sha256 -nodes   -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr"   -subj '/CN=pay21-preprod'
cat > "$TLS_DIR/server.ext" <<'EOF'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,DNS:pay21-redis,DNS:pay21-ingress,IP:127.0.0.1
EOF
openssl x509 -req -sha256 -days 1   -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key"   -CAcreateserial -out "$TLS_DIR/server.crt" -extfile "$TLS_DIR/server.ext"
chmod 0644 "$TLS_DIR/ca.crt" "$TLS_DIR/server.crt" "$TLS_DIR/server.key"

cat > "$REDIS_DIR/redis.conf" <<EOF
port 0
tls-port 6379
tls-cert-file /tls/server.crt
tls-key-file /tls/server.key
tls-ca-cert-file /tls/ca.crt
tls-auth-clients no
requirepass $REDIS_PASSWORD
appendonly yes
appendfsync everysec
save 60 1
maxmemory 128mb
maxmemory-policy noeviction
EOF

# The runner uses umask 077, while Redis and the production application image
# run as non-root users. Keep the generated material read-only but make the
# bind-mount directories traversable by those container identities.
chmod 0755 "$TLS_DIR" "$REDIS_DIR" "$NGINX_DIR"
chmod 0644 "$REDIS_DIR/redis.conf" "$TLS_DIR/ca.crt" "$TLS_DIR/server.crt" "$TLS_DIR/server.key"

docker run -d --name pay21-redis --network "$NET"   -v "$TLS_DIR:/tls:ro" -v "$REDIS_DIR:/config:ro"   redis:7-alpine redis-server /config/redis.conf >/dev/null

for attempt in $(seq 1 60); do
  if docker exec -e REDISCLI_AUTH="$REDIS_PASSWORD" pay21-redis       redis-cli --tls --cacert /tls/ca.crt -h pay21-redis -p 6379 ping 2>/dev/null       | grep -qx PONG; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    docker logs pay21-redis
    exit 1
  fi
  sleep 1
done

docker build --pull=false -t "$IMAGE" . >/tmp/pay21-docker-build.log 2>&1
IMAGE_ID="$(docker image inspect "$IMAGE" -f '{{.Id}}')"
test -n "$IMAGE_ID"
test "$(docker image inspect "$IMAGE" -f '{{.Config.User}}')" = "10001:10001"

DB_HOST="$GATEWAY"
API_DB="postgresql+asyncpg://pay21_api_runtime:$API_PASSWORD@$DB_HOST:55432/$DB"
AUTH_DB="postgresql+asyncpg://pay21_auth_runtime:$AUTH_PASSWORD@$DB_HOST:55432/$DB"
WORKER_DB="postgresql+asyncpg://pay21_worker_runtime:$WORKER_PASSWORD@$DB_HOST:55432/$DB"
REDISS_BASE="rediss://:$REDIS_PASSWORD@pay21-redis:6379"
REDISS_QUERY="ssl_cert_reqs=required&ssl_ca_certs=/run/pay21-tls/ca.crt"
REDIS_URL="$REDISS_BASE/0?$REDISS_QUERY"
CELERY_BROKER_URL="$REDISS_BASE/1?$REDISS_QUERY"
CELERY_RESULT_BACKEND="$REDISS_BASE/2?$REDISS_QUERY"

docker run -d --name pay21-api --network "$NET"   --add-host=host.docker.internal:host-gateway   -v "$TLS_DIR:/run/pay21-tls:ro"   -e ENVIRONMENT=production   -e DOERS_PROCESS_PROFILE=api   -e DATABASE_URL="$API_DB"   -e AUTH_DATABASE_URL="$AUTH_DB"   -e REDIS_URL="$REDIS_URL"   -e CELERY_BROKER_URL="$CELERY_BROKER_URL"   -e CELERY_RESULT_BACKEND="$CELERY_RESULT_BACKEND"   -e SECRET_KEY="$APP_SECRET"   -e AWS_ACCESS_KEY_ID=pay21-preprod-synthetic   -e AWS_SECRET_ACCESS_KEY=pay21-preprod-synthetic   -e AWS_REGION_NAME=us-east-1   -e S3_BUCKET_NAME=pay21-preprod-synthetic   -e NOTIFICATION_EMAIL_PROVIDER_MODE=disabled   -e SEARCH_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_CHECKOUT=false   -e PLATFORM_BILLING_WEBHOOK_PROCESSING=false   -e PLATFORM_BILLING_DUNNING_TRANSITIONS=false   -e PLATFORM_BILLING_NOTIFICATIONS=false   -e DOERS_PRESTOP_CONTROL_TOKEN="$PRESTOP_TOKEN"   -e P8_METRICS_OTLP_ENDPOINT="$OTLP_ENDPOINT"   -e P8_METRICS_EXPORT_INTERVAL_SECONDS=1   -e P8_METRICS_EXPORT_TIMEOUT_SECONDS=2   -e LOG_LEVEL=warning   "$IMAGE" >/dev/null

docker run -d --name pay21-worker --network "$NET"   --add-host=host.docker.internal:host-gateway   -v "$TLS_DIR:/run/pay21-tls:ro"   -e ENVIRONMENT=production   -e DOERS_PROCESS_PROFILE=worker   -e CELERY_WORKER_PROFILE=worker   -e WORKER_DATABASE_URL="$WORKER_DB"   -e REDIS_URL="$REDIS_URL"   -e CELERY_BROKER_URL="$CELERY_BROKER_URL"   -e CELERY_RESULT_BACKEND="$CELERY_RESULT_BACKEND"   -e SECRET_KEY="$APP_SECRET"   -e AWS_ACCESS_KEY_ID=pay21-preprod-synthetic   -e AWS_SECRET_ACCESS_KEY=pay21-preprod-synthetic   -e AWS_REGION_NAME=us-east-1   -e S3_BUCKET_NAME=pay21-preprod-synthetic   -e NOTIFICATION_EMAIL_PROVIDER_MODE=disabled   -e SEARCH_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_CHECKOUT=false   -e PLATFORM_BILLING_WEBHOOK_PROCESSING=false   -e PLATFORM_BILLING_DUNNING_TRANSITIONS=false   -e PLATFORM_BILLING_NOTIFICATIONS=false   -e P8_METRICS_OTLP_ENDPOINT="$OTLP_ENDPOINT"   -e P8_METRICS_EXPORT_INTERVAL_SECONDS=1   -e P8_METRICS_EXPORT_TIMEOUT_SECONDS=2   -e LOG_LEVEL=warning   "$IMAGE"   python -m celery -A app.core.celery_app:celery_app worker     --pool=solo --concurrency=1 --queues=pay21-preprod     --hostname=pay21-preprod@%h --without-gossip --without-mingle --loglevel=WARNING   >/dev/null

write_nginx_target() {
  local target="$1"
  cat > "$NGINX_DIR/default.conf" <<EOF
server {
  listen 8443 ssl;
  server_name localhost;
  ssl_certificate /tls/server.crt;
  ssl_certificate_key /tls/server.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  location / {
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_pass http://${target}:8000;
  }
}
EOF
}

write_nginx_target pay21-api
chmod 0644 "$NGINX_DIR/default.conf"
docker run -d --name pay21-ingress --network "$NET" -p 127.0.0.1:8443:8443   -v "$TLS_DIR:/tls:ro" -v "$NGINX_DIR:/etc/nginx/conf.d:ro"   nginx:1.27-alpine >/dev/null

for attempt in $(seq 1 120); do
  code="$(curl --cacert "$TLS_DIR/ca.crt" -sS -o /tmp/pay21-ready.json -w '%{http_code}'     https://localhost:8443/_system/ready 2>/dev/null || true)"
  if [ "$code" = "200" ] && grep -q '"ready"' /tmp/pay21-ready.json; then
    break
  fi
  if ! docker inspect -f '{{.State.Running}}' pay21-api 2>/dev/null | grep -qx true; then
    docker logs pay21-api
    exit 1
  fi
  if [ "$attempt" -eq 120 ]; then
    docker logs pay21-api
    docker logs pay21-ingress
    exit 1
  fi
  sleep 1
done

for attempt in $(seq 1 60); do
  if docker exec pay21-worker       python -m celery -A app.core.celery_app:celery_app inspect ping --timeout=3 2>/dev/null       | grep -q pong; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    docker logs pay21-worker
    exit 1
  fi
  sleep 1
done

# Prove a bad deployment fails readiness and the TLS router can restore the
# exact current-head last-known-good container without schema or image rollback.
BAD_REDIS_URL="rediss://:$REDIS_PASSWORD@pay21-missing-redis:6379/0?$REDISS_QUERY"
BAD_BROKER_URL="rediss://:$REDIS_PASSWORD@pay21-missing-redis:6379/1?$REDISS_QUERY"
BAD_RESULT_URL="rediss://:$REDIS_PASSWORD@pay21-missing-redis:6379/2?$REDISS_QUERY"
docker run -d --name pay21-bad-api --network "$NET" \
  --add-host=host.docker.internal:host-gateway \
  -v "$TLS_DIR:/run/pay21-tls:ro" \
  -e ENVIRONMENT=production \
  -e DOERS_PROCESS_PROFILE=api \
  -e DATABASE_URL="$API_DB" \
  -e AUTH_DATABASE_URL="$AUTH_DB" \
  -e REDIS_URL="$BAD_REDIS_URL" \
  -e CELERY_BROKER_URL="$BAD_BROKER_URL" \
  -e CELERY_RESULT_BACKEND="$BAD_RESULT_URL" \
  -e SECRET_KEY="$APP_SECRET" \
  -e AWS_ACCESS_KEY_ID=pay21-preprod-synthetic \
  -e AWS_SECRET_ACCESS_KEY=pay21-preprod-synthetic \
  -e AWS_REGION_NAME=us-east-1 \
  -e S3_BUCKET_NAME=pay21-preprod-synthetic \
  -e NOTIFICATION_EMAIL_PROVIDER_MODE=disabled \
  -e SEARCH_PROVIDER_MODE=disabled \
  -e PLATFORM_BILLING_PROVIDER_MODE=disabled \
  -e PLATFORM_BILLING_CHECKOUT=false \
  -e PLATFORM_BILLING_WEBHOOK_PROCESSING=false \
  -e PLATFORM_BILLING_DUNNING_TRANSITIONS=false \
  -e PLATFORM_BILLING_NOTIFICATIONS=false \
  -e DOERS_PRESTOP_CONTROL_TOKEN="$PRESTOP_TOKEN" \
  -e P8_METRICS_OTLP_ENDPOINT="$OTLP_ENDPOINT" \
  -e P8_METRICS_EXPORT_INTERVAL_SECONDS=1 \
  -e P8_METRICS_EXPORT_TIMEOUT_SECONDS=2 \
  -e LOG_LEVEL=warning \
  "$IMAGE" >/dev/null

sleep 2
test "$(docker inspect pay21-bad-api -f '{{.Image}}')" = "$IMAGE_ID"
write_nginx_target pay21-bad-api
docker exec pay21-ingress nginx -t
docker exec pay21-ingress nginx -s reload
bad_code=""
for attempt in $(seq 1 30); do
  bad_code="$(curl --cacert "$TLS_DIR/ca.crt" -sS -o /tmp/pay21-bad-ready.json -w '%{http_code}' \
    https://localhost:8443/_system/ready 2>/dev/null || true)"
  if [ "$bad_code" != "200" ]; then
    break
  fi
  sleep 1
done
if [ "$bad_code" = "200" ]; then
  echo 'PAY-21 deliberately bad deployment unexpectedly became ready' >&2
  exit 1
fi
printf '%s\n' "$bad_code" > /tmp/pay21-bad-ready-status.txt

write_nginx_target pay21-api
docker exec pay21-ingress nginx -t
docker exec pay21-ingress nginx -s reload
rollback_ok=0
for attempt in $(seq 1 60); do
  code="$(curl --cacert "$TLS_DIR/ca.crt" -sS -o /tmp/pay21-rollback-ready.json -w '%{http_code}' \
    https://localhost:8443/_system/ready 2>/dev/null || true)"
  if [ "$code" = "200" ] && grep -q '"ready"' /tmp/pay21-rollback-ready.json; then
    rollback_ok=1
    break
  fi
  sleep 1
done
test "$rollback_ok" = "1"
docker rm -f pay21-bad-api >/dev/null
echo 'PAY21_BAD_DEPLOYMENT_ROLLBACK=PASS'

# Only the TLS ingress may publish a host port.
test -z "$(docker port pay21-api)"
test -z "$(docker port pay21-worker)"
test -z "$(docker port pay21-redis)"
docker port pay21-ingress 8443/tcp | grep -q '127.0.0.1:8443'

API_IMAGE="$(docker inspect pay21-api -f '{{.Image}}')"
WORKER_IMAGE="$(docker inspect pay21-worker -f '{{.Image}}')"
test "$API_IMAGE" = "$IMAGE_ID"
test "$WORKER_IMAGE" = "$IMAGE_ID"

python - "$PAY21_RUNTIME_EVIDENCE" "$PAY21_CANDIDATE_SHA" "$IMAGE_ID" <<'PY'
import json,sys
from pathlib import Path
path,candidate,image_id=sys.argv[1:]
record={
  "schema_version":1,
  "phase":"PAY-21",
  "candidate_sha":candidate,
  "postgresql_major":16,
  "redis_major":7,
  "redis_tls":True,
  "redis_authenticated":True,
  "celery_worker_real":True,
  "api_tls_ingress":True,
  "tls_validation_bypassed":False,
  "production_image_id":image_id,
  "api_worker_same_image":True,
  "production_process_profiles":["api","worker"],
  "production_db_roles":["api","auth","worker","maintenance","finance_config"],
  "private_service_network":True,
  "published_ports":["127.0.0.1:8443/tcp"],
  "production_customer_data":False,
  "bad_deployment_rollback":True,
  "decision":"PASS"
}
Path(path).write_text(json.dumps(record,indent=2,sort_keys=True)+"\n",encoding="utf-8")
print(json.dumps(record,indent=2,sort_keys=True))
PY

for attempt in $(seq 1 30); do
  if [ -s "$OTLP_CAPTURE" ]; then
    break
  fi
  sleep 1
done
test -s "$OTLP_CAPTURE"
echo 'PAY21_REAL_OTLP_EXPORT=PASS'
echo 'PAY21_REAL_POSTGRESQL=PASS'
echo 'PAY21_REAL_REDIS_TLS=PASS'
echo 'PAY21_REAL_CELERY=PASS'
echo 'PAY21_REAL_TLS_INGRESS=PASS'
echo 'PAY21_PRODUCTION_IMAGE=PASS'
echo 'PAY21_PRODUCTION_DB_ROLES=PASS'
echo 'PAY21_NETWORK_CONTROLS=PASS'
