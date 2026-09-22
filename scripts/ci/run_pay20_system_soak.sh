#!/usr/bin/env bash
set -euo pipefail

: "${P10S_CANDIDATE_SHA:?}" "${P10S_DB:?}" "${P10S_SECRET_KEY:?}"   "${P10S_METRICS_PORT:?}" "${P10S_DURATION_SECONDS:?}"   "${P10S_CPU_LIMIT:?}" "${EVIDENCE_DIR:?}"

WARMUP_SECONDS="${PAY20_SYSTEM_WARMUP_SECONDS:-120}"
if [ "${WARMUP_SECONDS}" -lt 60 ]; then
  echo "PAY-20 warmup must be at least 60 seconds" >&2
  exit 1
fi

mkdir -p "${EVIDENCE_DIR}"
test "$(git rev-parse HEAD)" = "${P10S_CANDIDATE_SHA}"

cleanup(){
  touch "${EVIDENCE_DIR}/stop" || true
  docker rm -f p10s-api >/dev/null 2>&1 || true
  for f in metrics worker sampler; do
    [ -s "/tmp/p10s-${f}.pid" ] && kill "$(cat "/tmp/p10s-${f}.pid")" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

python - "${P10S_CPU_LIMIT}" <<'PY'
import sys
limit=float(sys.argv[1])
if not 0 < limit <= 1.25:
    raise SystemExit("P10S_CPU_LIMIT must be in (0, 1.25] cores")
PY

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "${P10S_DB}"   -f scripts/ci/p9m_seed_populated_predecessor.sql >"${EVIDENCE_DIR}/p9m-seed.log"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "${P10S_DB}"   -f scripts/ci/p10b_seed_baseline.sql >"${EVIDENCE_DIR}/p10b-seed.log"
grep -q 'P10B_SYNTHETIC_BASELINE_SEED=PASS' "${EVIDENCE_DIR}/p10b-seed.log"

python -s scripts/ci/p8o_otlp_collector.py   --host 127.0.0.1 --port "${P10S_METRICS_PORT}"   --capture "${EVIDENCE_DIR}/runtime-metrics.jsonl"   >"${EVIDENCE_DIR}/metrics.log" 2>&1 &
echo $! >/tmp/p10s-metrics.pid
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:${P10S_METRICS_PORT}/healthz" >/dev/null && break
  sleep .25
done

IMAGE="doers-pay20s:${P10S_CANDIDATE_SHA}"
docker build --pull=false -t "${IMAGE}" . >"${EVIDENCE_DIR}/docker-build.log" 2>&1
docker run -d --name p10s-api --network host --cpus "${P10S_CPU_LIMIT}"   -e ENVIRONMENT=production -e DOERS_PROCESS_PROFILE=api   -e DATABASE_URL="postgresql+asyncpg://app_test_runtime:ci-app-test-runtime@127.0.0.1:5432/${P10S_DB}"   -e AUTH_DATABASE_URL="postgresql+asyncpg://auth_test_runtime:ci-auth-test-runtime@127.0.0.1:5432/${P10S_DB}"   -e REDIS_URL=redis://127.0.0.1:6379/0   -e CELERY_BROKER_URL=redis://127.0.0.1:6379/1   -e CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2   -e SECRET_KEY="${P10S_SECRET_KEY}"   -e AWS_ACCESS_KEY_ID=pay20-synthetic   -e AWS_SECRET_ACCESS_KEY=pay20-synthetic   -e S3_BUCKET_NAME=pay20-synthetic   -e NOTIFICATION_EMAIL_PROVIDER_MODE=disabled   -e SEARCH_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_PROVIDER_MODE=disabled   -e PLATFORM_BILLING_CHECKOUT=false   -e PLATFORM_BILLING_WEBHOOK_PROCESSING=false   -e PLATFORM_BILLING_DUNNING_TRANSITIONS=false   -e PLATFORM_BILLING_NOTIFICATIONS=false   -e P8_METRICS_OTLP_ENDPOINT="http://127.0.0.1:${P10S_METRICS_PORT}/v1/metrics"   -e P8_METRICS_EXPORT_INTERVAL_SECONDS=1   -e P8_METRICS_EXPORT_TIMEOUT_SECONDS=2   -e DOERS_PRESTOP_CONTROL_TOKEN=pay20-synthetic-control-token-0123456789abcdef0123456789abcdef   -e LOG_LEVEL=warning   "${IMAGE}" >"${EVIDENCE_DIR}/container-id.txt"

for _ in $(seq 1 120); do
  curl -fsS http://127.0.0.1:8000/_system/ready >"${EVIDENCE_DIR}/readiness.json" && break
  sleep .5
done
grep -q '"ready"' "${EVIDENCE_DIR}/readiness.json"

actual_nano_cpus="$(docker inspect -f '{{.HostConfig.NanoCpus}}' p10s-api)"
expected_nano_cpus="$(python - "${P10S_CPU_LIMIT}" <<'PY'
import sys
print(int(float(sys.argv[1]) * 1_000_000_000))
PY
)"
test "${actual_nano_cpus}" = "${expected_nano_cpus}"
printf '%s
' "${P10S_CPU_LIMIT}" >"${EVIDENCE_DIR}/cpu-limit-cores.txt"
printf '%s
' "${actual_nano_cpus}" >"${EVIDENCE_DIR}/cpu-limit-nanocpus.txt"
echo 'PAY20_SYSTEM_SOAK_CPU_ENVELOPE=PASS'

# Warm the production container, PostgreSQL plan/buffer state, Python hot paths,
# and connection pools without counting this interval toward the measured soak.
python -s scripts/ci/p10b_load_calibration.py   --base-url http://127.0.0.1:8000   --secret-key "${P10S_SECRET_KEY}"   --duration-seconds "${WARMUP_SECONDS}"   --concurrency 24   --tenants 8   --output "${EVIDENCE_DIR}/warmup.json"   | tee "${EVIDENCE_DIR}/warmup.log"
echo 'PAY20_SYSTEM_SOAK_WARMUP=PASS'

# Measured soak remains the complete frozen 300s P10-S window set.
python -s scripts/ci/p10s_worker_activity.py   --duration-seconds "${P10S_DURATION_SECONDS}"   --output "${EVIDENCE_DIR}/worker.json"   --log "${EVIDENCE_DIR}/worker.log"   >"${EVIDENCE_DIR}/worker-run.log" 2>&1 &
echo $! >/tmp/p10s-worker.pid

python -s scripts/ci/p10s_resource_sampler.py   --duration-seconds "${P10S_DURATION_SECONDS}"   --output "${EVIDENCE_DIR}/resources.jsonl"   >"${EVIDENCE_DIR}/sampler.log" 2>&1 &
echo $! >/tmp/p10s-sampler.pid

python -s scripts/ci/p10s_http_soak.py   --base-url http://127.0.0.1:8000   --secret-key "${P10S_SECRET_KEY}"   --duration-seconds "${P10S_DURATION_SECONDS}"   --window-seconds 60   --concurrency 24   --output "${EVIDENCE_DIR}/http.json"   | tee "${EVIDENCE_DIR}/http.log"

wait "$(cat /tmp/p10s-worker.pid)"
wait "$(cat /tmp/p10s-sampler.pid)"

python -s scripts/ci/p10s_verify_soak.py   --http "${EVIDENCE_DIR}/http.json"   --worker "${EVIDENCE_DIR}/worker.json"   --resources "${EVIDENCE_DIR}/resources.jsonl"   --output "${EVIDENCE_DIR}/decision.json"   | tee "${EVIDENCE_DIR}/decision.log"

grep -q '^P10_SOAK=PASS$' "${EVIDENCE_DIR}/decision.log"
test "$(git rev-parse HEAD)" = "${P10S_CANDIDATE_SHA}"
git diff --exit-code
git diff --cached --exit-code

echo 'PAY20_SYSTEM_SOAK_MEASURED_300S=PASS'
