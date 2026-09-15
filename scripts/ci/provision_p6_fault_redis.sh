#!/usr/bin/env bash
set -euo pipefail

if [[ "${P5D_PROCESS_FAULTS:-0}" == "1" ]]; then
  PREFIX="p5d"
elif [[ "${P5W2_PROCESS_FAULTS:-0}" == "1" ]]; then
  PREFIX="p5w2"
else
  echo "P6 fault Redis bootstrap requires P5D_PROCESS_FAULTS=1 or P5W2_PROCESS_FAULTS=1" >&2
  exit 1
fi

if [[ -z "${GITHUB_ENV:-}" || -z "${GITHUB_WORKSPACE:-}" ]]; then
  echo "P6 fault Redis bootstrap is restricted to the explicit GitHub Actions fault jobs" >&2
  exit 1
fi

sudo sysctl -w vm.overcommit_memory=1 >/dev/null
test "$(cat /proc/sys/vm/overcommit_memory)" = "1"

# The inherited workflows provide Redis as a GitHub service container, not as a
# host package. Install only the CLI client needed to prove the replacement
# broker's authenticated plaintext controller surface and authenticated TLS
# production-worker surface.
sudo apt-get install -y redis-tools >/dev/null

mapfile -t service_ids < <(docker ps -aq --filter ancestor=redis:7-alpine)
if [[ "${#service_ids[@]}" -ne 1 ]]; then
  echo "Expected exactly one inherited redis:7-alpine service before P6 replacement; found ${#service_ids[@]}" >&2
  exit 1
fi
docker rm -f "${service_ids[0]}" >/dev/null

CERT_DIR="/tmp/${PREFIX}-p6-fault-redis-tls"
CONFIG_DIR="/tmp/${PREFIX}-p6-fault-redis-config"
CONTAINER="${PREFIX}-p6-fault-redis"
VOLUME="${PREFIX}-p6-fault-redis-data"
mkdir -p "${CERT_DIR}" "${CONFIG_DIR}"

openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
  -keyout "${CERT_DIR}/ca.key" \
  -out "${CERT_DIR}/ca.crt" \
  -subj "/CN=DOERS ${PREFIX} inherited-fault CI CA" >/dev/null 2>&1
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "${CERT_DIR}/server.key" \
  -out "${CERT_DIR}/server.csr" \
  -subj "/CN=localhost" >/dev/null 2>&1
cat > "${CERT_DIR}/server.ext" <<'EOF'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req -sha256 -days 1 \
  -in "${CERT_DIR}/server.csr" \
  -CA "${CERT_DIR}/ca.crt" \
  -CAkey "${CERT_DIR}/ca.key" \
  -CAcreateserial \
  -out "${CERT_DIR}/server.crt" \
  -extfile "${CERT_DIR}/server.ext" >/dev/null 2>&1
chmod 0644 "${CERT_DIR}/ca.crt" "${CERT_DIR}/server.crt" "${CERT_DIR}/server.key"

CONTROLLER_PASSWORD="$(openssl rand -hex 24)"
WORKER_PASSWORD="$(openssl rand -hex 24)"
REPLICA_PASSWORD="$(openssl rand -hex 24)"
echo "::add-mask::${CONTROLLER_PASSWORD}"
echo "::add-mask::${WORKER_PASSWORD}"
echo "::add-mask::${REPLICA_PASSWORD}"

cat > "${CONFIG_DIR}/primary.conf" <<EOF
bind 0.0.0.0
protected-mode no
port 6379
tls-port 6380
tls-cert-file /tls/server.crt
tls-key-file /tls/server.key
tls-ca-cert-file /tls/ca.crt
tls-auth-clients no
dir /data/primary
appendonly yes
appendfsync everysec
save 60 1
maxmemory 128mb
maxmemory-policy noeviction
user default off
user faultcontroller on >${CONTROLLER_PASSWORD} ~* &* +@all
user faultworker on >${WORKER_PASSWORD} ~* &* +@all
user replicator on >${REPLICA_PASSWORD} ~* &* +@all
EOF

cat > "${CONFIG_DIR}/replica.conf" <<EOF
bind 127.0.0.1
protected-mode no
port 6381
tls-port 6382
tls-cert-file /tls/server.crt
tls-key-file /tls/server.key
tls-ca-cert-file /tls/ca.crt
tls-auth-clients no
replicaof 127.0.0.1 6380
tls-replication yes
masteruser replicator
masterauth ${REPLICA_PASSWORD}
dir /data/replica
appendonly yes
appendfsync everysec
save 60 1
maxmemory 128mb
maxmemory-policy noeviction
user default off
user faultcontroller on >${CONTROLLER_PASSWORD} ~* &* +@all
user faultworker on >${WORKER_PASSWORD} ~* &* +@all
user replicator on >${REPLICA_PASSWORD} ~* &* +@all
EOF

docker volume create "${VOLUME}" >/dev/null
docker run -d --name "${CONTAINER}" \
  -p 6379:6379 \
  -p 16379:6380 \
  -v "${VOLUME}:/data" \
  -v "${CERT_DIR}:/tls:ro" \
  -v "${CONFIG_DIR}:/config:ro" \
  redis:7-alpine \
  sh -c 'mkdir -p /data/primary /data/replica; redis-server /config/primary.conf & exec redis-server /config/replica.conf' \
  >/dev/null

CONTROLLER_CLI=(redis-cli --user faultcontroller -h 127.0.0.1 -p 6379)
for _ in $(seq 1 80); do
  if REDISCLI_AUTH="${CONTROLLER_PASSWORD}" "${CONTROLLER_CLI[@]}" ping 2>/dev/null | grep -qx PONG; then
    break
  fi
  sleep 0.25
done
REDISCLI_AUTH="${CONTROLLER_PASSWORD}" "${CONTROLLER_CLI[@]}" ping | grep -qx PONG

set +e
BAD_CONTROLLER_OUTPUT="$(REDISCLI_AUTH='definitely-wrong-controller-password' "${CONTROLLER_CLI[@]}" ping 2>&1)"
set -e
if [[ "${BAD_CONTROLLER_OUTPUT}" == *PONG* ]]; then
  echo "P6 fault-controller plaintext Redis accepted an invalid password" >&2
  exit 1
fi
if [[ "${BAD_CONTROLLER_OUTPUT}" != *WRONGPASS* && "${BAD_CONTROLLER_OUTPUT}" != *NOAUTH* ]]; then
  echo "P6 fault-controller invalid-password probe did not fail as an authentication error" >&2
  exit 1
fi

TLS_CLI=(redis-cli --tls --cacert "${CERT_DIR}/ca.crt" --user faultworker -h localhost -p 16379)
REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" ping | grep -qx PONG
set +e
BAD_WORKER_OUTPUT="$(REDISCLI_AUTH='definitely-wrong-worker-password' "${TLS_CLI[@]}" ping 2>&1)"
set -e
if [[ "${BAD_WORKER_OUTPUT}" == *PONG* ]]; then
  echo "P6 fault-worker TLS Redis accepted an invalid password" >&2
  exit 1
fi
if [[ "${BAD_WORKER_OUTPUT}" != *WRONGPASS* && "${BAD_WORKER_OUTPUT}" != *NOAUTH* ]]; then
  echo "P6 fault-worker invalid-password probe did not fail as an authentication error" >&2
  exit 1
fi

for _ in $(seq 1 80); do
  connected="$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw INFO replication 2>/dev/null | awk -F: '/^connected_slaves:/ {gsub("\r", "", $2); print $2}')"
  if [[ "${connected}" == "1" ]]; then
    break
  fi
  sleep 0.25
done
test "$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw INFO replication | awk -F: '/^connected_slaves:/ {gsub("\r", "", $2); print $2}')" = "1"
test "$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw CONFIG GET appendonly | tail -n 1)" = "yes"
test "$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw CONFIG GET appendfsync | tail -n 1)" = "everysec"
test "$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw CONFIG GET maxmemory-policy | tail -n 1)" = "noeviction"
test -n "$(REDISCLI_AUTH="${WORKER_PASSWORD}" "${TLS_CLI[@]}" --raw CONFIG GET save | tail -n 1)"

CONTROLLER_BASE="redis://faultcontroller:${CONTROLLER_PASSWORD}@127.0.0.1:6379"
REDISS_BASE="rediss://faultworker:${WORKER_PASSWORD}@localhost:16379"
REDISS_QUERY="ssl_cert_reqs=required&ssl_ca_certs=${CERT_DIR}/ca.crt"
{
  # Later pytest-controller steps keep their original plaintext broker-fault
  # surface, now authenticated. Only the spawned production worker is remapped
  # to the independently authenticated TLS endpoint by sitecustomize.py.
  echo "REDIS_URL=${CONTROLLER_BASE}/0"
  echo "CELERY_BROKER_URL=${CONTROLLER_BASE}/1"
  echo "CELERY_RESULT_BACKEND=${CONTROLLER_BASE}/2"
  echo "P6_FAULT_WORKER_REDIS_URL=${REDISS_BASE}/0?${REDISS_QUERY}"
  echo "P6_FAULT_WORKER_CELERY_BROKER_URL=${REDISS_BASE}/1?${REDISS_QUERY}"
  echo "P6_FAULT_WORKER_CELERY_RESULT_BACKEND=${REDISS_BASE}/2?${REDISS_QUERY}"
  echo "REDIS_PRODUCTION_TOPOLOGY=self_managed_ha"
  echo "REDIS_PERSISTENCE_MODE=aof_everysec_rdb"
  echo "REDIS_MAXMEMORY_POLICY=noeviction"
  echo "REDIS_HA_MIN_REPLICAS=1"
  echo "REDIS_VM_OVERCOMMIT_MEMORY=1"
  echo "REDIS_MANAGED_PROVIDER_ATTESTED=false"
  echo "PYTHONPATH=${GITHUB_WORKSPACE}/scripts/ci/p6_fault_worker_bootstrap"
} >> "${GITHUB_ENV}"

echo "P6_FAULT_CONTROLLER_AUTH=PASS"
echo "P6_FAULT_WORKER_REDIS_TLS_AUTH_HA=PASS"
