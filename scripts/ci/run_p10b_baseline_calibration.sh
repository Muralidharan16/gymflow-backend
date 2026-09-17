#!/usr/bin/env bash
set -euo pipefail

: "${P10B_CANDIDATE_SHA:?}"
: "${P10B_ALEMBIC_HEAD:?}"
: "${P10B_DB:?}"
: "${P10B_SECRET_KEY:?}"
: "${P10B_METRICS_PORT:?}"
: "${P10B_API_PORT:?}"
: "${P10B_DURATION_SECONDS:?}"
: "${P10B_CONCURRENCY:?}"
: "${EVIDENCE_DIR:?}"

mkdir -p "$EVIDENCE_DIR"
test "$(git rev-parse HEAD)" = "$P10B_CANDIDATE_SHA"
test "$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')" = "$P10B_ALEMBIC_HEAD"

cleanup() {
  touch "$EVIDENCE_DIR/load.done" >/dev/null 2>&1 || true
  docker rm -f p10b-api >/dev/null 2>&1 || true
  if [ -s /tmp/p10b-metrics.pid ]; then
    kill "$(cat /tmp/p10b-metrics.pid)" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Build on the already-migrated real PG16 database prepared by the workflow.
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -f scripts/ci/p9m_seed_populated_predecessor.sql \
  | tee "$EVIDENCE_DIR/p9m-seed.log"
grep -q 'P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS' "$EVIDENCE_DIR/p9m-seed.log"

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -f scripts/ci/p10b_seed_baseline.sql \
  | tee "$EVIDENCE_DIR/p10b-seed.log"
grep -q 'P10B_SYNTHETIC_BASELINE_SEED=PASS' "$EVIDENCE_DIR/p10b-seed.log"

sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/p9-protected-before.txt"

# Production runtime observability is fail-closed, so provide the existing
# synthetic loopback OTLP collector rather than disabling the requirement.
python -s scripts/ci/p8o_otlp_collector.py \
  --host 127.0.0.1 \
  --port "$P10B_METRICS_PORT" \
  --capture "$EVIDENCE_DIR/runtime-metrics.jsonl" \
  > "$EVIDENCE_DIR/metrics-collector.log" 2>&1 &
echo "$!" > /tmp/p10b-metrics.pid
for _ in $(seq 1 40); do
  if curl --fail --silent "http://127.0.0.1:${P10B_METRICS_PORT}/healthz" >/dev/null; then
    break
  fi
  sleep 0.25
done
curl --fail --silent "http://127.0.0.1:${P10B_METRICS_PORT}/healthz" \
  > "$EVIDENCE_DIR/metrics-health.txt"

echo 'P10B_PRODUCTION_METRICS_BOUNDARY=PASS'

IMAGE_TAG="doers-p10b:${P10B_CANDIDATE_SHA}"
docker build --pull=false --tag "$IMAGE_TAG" . \
  > "$EVIDENCE_DIR/docker-build.log" 2>&1
IMAGE_ID="$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')"
printf '%s\n' "$IMAGE_ID" > "$EVIDENCE_DIR/image-id.txt"
docker image inspect "$IMAGE_TAG" > "$EVIDENCE_DIR/image-inspect.json"

# Record current image posture. P10-B observes the production image; P10-H owns
# the later hardening gate and must remove any root/healthcheck deficiencies.
python - "$EVIDENCE_DIR/image-inspect.json" "$EVIDENCE_DIR/image-posture.json" <<'PY'
import json, sys
from pathlib import Path
raw = json.loads(Path(sys.argv[1]).read_text())[0]
config = raw.get('Config') or {}
posture = {
    'user': config.get('User') or 'root(default)',
    'healthcheck': config.get('Healthcheck'),
    'exposed_ports': sorted((config.get('ExposedPorts') or {}).keys()),
    'cmd': config.get('Cmd'),
    'entrypoint': config.get('Entrypoint'),
}
Path(sys.argv[2]).write_text(json.dumps(posture, indent=2, sort_keys=True) + '\n')
PY

docker rm -f p10b-api >/dev/null 2>&1 || true
docker run --detach --name p10b-api --network host \
  --env ENVIRONMENT=production \
  --env DOERS_PROCESS_PROFILE=api \
  --env DATABASE_URL="postgresql+asyncpg://app_test_runtime:ci-app-test-runtime@127.0.0.1:5432/${P10B_DB}" \
  --env AUTH_DATABASE_URL="postgresql+asyncpg://auth_test_runtime:ci-auth-test-runtime@127.0.0.1:5432/${P10B_DB}" \
  --env REDIS_URL=redis://127.0.0.1:6379/0 \
  --env CELERY_BROKER_URL=redis://127.0.0.1:6379/1 \
  --env CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2 \
  --env SECRET_KEY="$P10B_SECRET_KEY" \
  --env AWS_ACCESS_KEY_ID=p10b-synthetic \
  --env AWS_SECRET_ACCESS_KEY=p10b-synthetic \
  --env S3_BUCKET_NAME=p10b-synthetic \
  --env NOTIFICATION_EMAIL_PROVIDER_MODE=disabled \
  --env SEARCH_PROVIDER_MODE=disabled \
  --env PLATFORM_BILLING_PROVIDER_MODE=disabled \
  --env PLATFORM_BILLING_CHECKOUT=false \
  --env PLATFORM_BILLING_WEBHOOK_PROCESSING=false \
  --env PLATFORM_BILLING_DUNNING_TRANSITIONS=false \
  --env PLATFORM_BILLING_NOTIFICATIONS=false \
  --env P8_METRICS_OTLP_ENDPOINT="http://127.0.0.1:${P10B_METRICS_PORT}/v1/metrics" \
  --env P8_METRICS_EXPORT_INTERVAL_SECONDS=1 \
  --env P8_METRICS_EXPORT_TIMEOUT_SECONDS=2 \
  --env DOERS_PRESTOP_CONTROL_TOKEN=p10b-synthetic-control-token-0123456789abcdef0123456789abcdef \
  --env LOG_LEVEL=warning \
  "$IMAGE_TAG" \
  > "$EVIDENCE_DIR/container-id.txt"

ready=0
for _ in $(seq 1 120); do
  if curl --fail --silent "http://127.0.0.1:${P10B_API_PORT}/_system/ready" \
      > "$EVIDENCE_DIR/readiness.json"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  docker logs p10b-api > "$EVIDENCE_DIR/container.log" 2>&1 || true
  cat "$EVIDENCE_DIR/container.log" >&2 || true
  exit 1
fi
python - "$EVIDENCE_DIR/readiness.json" <<'PY'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()) == {'status': 'ready'}
PY
echo 'P10B_PRODUCTION_CONTAINER_READY=PASS'

# Live process evidence before calibration. Use the locked Python Redis client
# instead of relying on a mutable host redis-cli package.
docker top p10b-api -eo pid,user,comm,args > "$EVIDENCE_DIR/container-processes.txt"
docker logs p10b-api > "$EVIDENCE_DIR/container-startup.log" 2>&1 || true
python -s - "$EVIDENCE_DIR/redis-ping.txt" <<'PY'
import os, sys
from pathlib import Path
import redis

client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
assert client.ping() is True
Path(sys.argv[1]).write_text('PONG\n')
PY
grep -qx 'PONG' "$EVIDENCE_DIR/redis-ping.txt"
sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -c "SELECT version(); SELECT count(*) FROM pg_stat_activity WHERE datname='${P10B_DB}';" \
  > "$EVIDENCE_DIR/postgres-before.txt"

rm -f "$EVIDENCE_DIR/load.done"
(
  while [ ! -f "$EVIDENCE_DIR/load.done" ]; do
    printf '%s\t' "$(date +%s%3N)"
    docker stats --no-stream --format '{{json .}}' p10b-api || true
    sleep 2
  done
) > "$EVIDENCE_DIR/docker-stats.jsonl" &
STATS_PID="$!"

set +e
python -s scripts/ci/p10b_load_calibration.py \
  --base-url "http://127.0.0.1:${P10B_API_PORT}" \
  --secret-key "$P10B_SECRET_KEY" \
  --duration-seconds "$P10B_DURATION_SECONDS" \
  --concurrency "$P10B_CONCURRENCY" \
  --tenants 8 \
  --output "$EVIDENCE_DIR/http-calibration.json" \
  | tee "$EVIDENCE_DIR/http-calibration.log"
LOAD_RC=${PIPESTATUS[0]}
set -e
touch "$EVIDENCE_DIR/load.done"
wait "$STATS_PID" || true
if [ "$LOAD_RC" -ne 0 ]; then
  docker logs p10b-api > "$EVIDENCE_DIR/container-after-load-failure.log" 2>&1 || true
  exit "$LOAD_RC"
fi
grep -q 'P10B_CALIBRATION_HTTP=PASS' "$EVIDENCE_DIR/http-calibration.log"

python - "$EVIDENCE_DIR/docker-stats.jsonl" "$EVIDENCE_DIR/resource-calibration.json" <<'PY'
import json, re, statistics, sys
from pathlib import Path

def bytes_value(raw: str) -> float:
    text = raw.strip()
    match = re.fullmatch(r'([0-9.]+)([KMGTP]?i?B)', text)
    if not match:
        raise ValueError(text)
    value = float(match.group(1))
    unit = match.group(2)
    scale = {
        'B': 1, 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'TB': 1000**4,
        'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4,
    }[unit]
    return value * scale

cpus, rss, pids = [], [], []
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line.strip():
        continue
    _, raw = line.split('\t', 1)
    item = json.loads(raw)
    cpus.append(float(item['CPUPerc'].rstrip('%')))
    used = item['MemUsage'].split('/', 1)[0].strip()
    rss.append(bytes_value(used))
    pids.append(int(item['PIDs']))
if not cpus:
    raise SystemExit('no docker resource samples captured')
summary = {
    'schema_version': 1,
    'samples': len(cpus),
    'cpu_percent': {
        'avg': round(statistics.fmean(cpus), 3),
        'max': round(max(cpus), 3),
    },
    'rss_bytes': {
        'avg': round(statistics.fmean(rss)),
        'max': round(max(rss)),
        'first': round(rss[0]),
        'last': round(rss[-1]),
    },
    'pids': {'max': max(pids)},
}
Path(sys.argv[2]).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps(summary, indent=2, sort_keys=True))
PY

sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -c "SELECT count(*) FROM pg_stat_activity WHERE datname='${P10B_DB}'; SELECT xact_commit, xact_rollback, blks_read, blks_hit FROM pg_stat_database WHERE datname='${P10B_DB}';" \
  > "$EVIDENCE_DIR/postgres-after.txt"
python -s - "$EVIDENCE_DIR/redis-stats.txt" "$EVIDENCE_DIR/redis-memory.txt" <<'PY'
import json, os, sys
from pathlib import Path
import redis

client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
Path(sys.argv[1]).write_text(json.dumps(client.info('stats'), indent=2, sort_keys=True) + '\n')
Path(sys.argv[2]).write_text(json.dumps(client.info('memory'), indent=2, sort_keys=True) + '\n')
PY

# The real write path must have advanced synchronized tenant counters without
# duplicate member numbers.
sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P10B_DB" <<'SQL' \
  > "$EVIDENCE_DIR/member-write-integrity.txt"
WITH active_orgs AS (
    SELECT md5('p9m-org-' || g::text)::uuid AS org_id
    FROM generate_series(1, 8) AS g
), checks AS (
    SELECT
        o.org_id,
        count(m.id) AS member_count,
        count(DISTINCT m.member_number) AS distinct_numbers,
        max(m.member_number) AS max_number,
        c.current_value AS counter_value
    FROM active_orgs o
    JOIN public.members m ON m.org_id = o.org_id
    JOIN public.organization_counters c
      ON c.org_id = o.org_id AND c.counter_key = 'member'
    GROUP BY o.org_id, c.current_value
)
SELECT
    count(*) FILTER (WHERE member_count > 500),
    count(*) FILTER (WHERE member_count = distinct_numbers),
    count(*) FILTER (WHERE max_number = counter_value)
FROM checks;
SQL
grep -Eq '^[1-8]\|8\|8$' "$EVIDENCE_DIR/member-write-integrity.txt"
echo 'P10B_REAL_WRITE_COUNTER_INTEGRITY=PASS'

sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P10B_DB" \
  -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/p9-protected-after.txt"
cmp "$EVIDENCE_DIR/p9-protected-before.txt" "$EVIDENCE_DIR/p9-protected-after.txt"
echo 'P10B_P9_PROTECTED_BASELINE_UNCHANGED=PASS'

python - "$EVIDENCE_DIR" "$P10B_CANDIDATE_SHA" "$P10B_ALEMBIC_HEAD" "$IMAGE_ID" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
http = json.loads((root / 'http-calibration.json').read_text())
resources = json.loads((root / 'resource-calibration.json').read_text())
image_posture = json.loads((root / 'image-posture.json').read_text())
record = {
    'schema_version': 1,
    'phase': 'P10-B',
    'mode': 'calibration',
    'candidate_sha': sys.argv[2],
    'alembic_head': sys.argv[3],
    'postgresql_major': 16,
    'real_redis': True,
    'production_profile': 'api',
    'production_container': True,
    'synthetic_only': True,
    'live_provider_credentials_present': False,
    'image_id': sys.argv[4],
    'image_posture': image_posture,
    'http': http,
    'resources': resources,
    'p9_protected_baseline_unchanged': True,
    'budgets_frozen': False,
    'decision': 'CALIBRATION_PASS',
}
encoded = json.dumps(record, indent=2, sort_keys=True) + '\n'
(root / 'decision.json').write_text(encoded)
(root / 'decision.sha256').write_text(
    hashlib.sha256(encoded.encode()).hexdigest() + '  decision.json\n'
)
print(json.dumps(record, indent=2, sort_keys=True))
PY

test "$(git rev-parse HEAD)" = "$P10B_CANDIDATE_SHA"
git diff --exit-code
git diff --cached --exit-code
echo 'P10B_CALIBRATION=PASS'
echo 'P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED'
