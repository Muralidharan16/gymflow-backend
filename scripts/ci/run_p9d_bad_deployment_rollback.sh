#!/usr/bin/env bash
set -euo pipefail

: "${P9D_CANDIDATE_SHA:?}"
: "${P9D_LAST_KNOWN_GOOD_SHA:?}"
: "${P9D_PREDECESSOR:?}"
: "${P9D_HEAD:?}"
: "${P9D_DB:?}"
: "${P9D_LKG_SOURCE:?}"
: "${P9D_LKG_RUNTIME:?}"
: "${P9D_CANDIDATE_RUNTIME:?}"
: "${P9D_LKG_WORK:?}"
: "${P9D_CANDIDATE_WORK:?}"
: "${P9D_HOME:?}"
: "${P9D_LKG_PORT:?}"
: "${P9D_CANDIDATE_PORT:?}"
: "${P9D_ROUTER_PORT:?}"
: "${P9D_BAD_REDIS_PORT:?}"
: "${P9D_METRICS_PORT:?}"
: "${P9D_API_LOGIN:?}"
: "${EVIDENCE_DIR:?}"
: "${GITHUB_WORKSPACE:?}"
: "${REDIS_URL:?}"
: "${CELERY_BROKER_URL:?}"
: "${CELERY_RESULT_BACKEND:?}"

mkdir -p "$EVIDENCE_DIR"
test "$(git rev-parse HEAD)" = "$P9D_CANDIDATE_SHA"
git merge-base --is-ancestor "$P9D_LAST_KNOWN_GOOD_SHA" HEAD

rm -rf "$P9D_LKG_SOURCE"
git worktree add --detach "$P9D_LKG_SOURCE" "$P9D_LAST_KNOWN_GOOD_SHA"
test "$(cd "$P9D_LKG_SOURCE" && git rev-parse HEAD)" = "$P9D_LAST_KNOWN_GOOD_SHA"

P9D_LKG_VENV=/tmp/p9d-lkg-venv
rm -rf "$P9D_LKG_VENV"
python -m venv "$P9D_LKG_VENV"
"$P9D_LKG_VENV/bin/python" -m pip install 'pip==26.2.1'
"$P9D_LKG_VENV/bin/python" -m pip install -r "$P9D_LKG_SOURCE/requirements-test.lock"
"$P9D_LKG_VENV/bin/python" -m pip check
"$P9D_LKG_VENV/bin/python" -m pip freeze | LC_ALL=C sort > "$EVIDENCE_DIR/lkg-runtime-resolved.txt"
diff -u "$P9D_LKG_SOURCE/requirements-test.lock" "$EVIDENCE_DIR/lkg-runtime-resolved.txt"
echo 'P9D_HISTORICAL_DEPENDENCY_RUNTIME=PASS'

lkg_head="$(cd "$P9D_LKG_SOURCE" && python -s -m alembic -c alembic.ini heads | awk '{print $1}')"
candidate_head="$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')"
test "$lkg_head" = "$P9D_HEAD"
test "$candidate_head" = "$P9D_HEAD"
{
  printf 'candidate_sha=%s\n' "$P9D_CANDIDATE_SHA"
  printf 'last_known_good_sha=%s\n' "$P9D_LAST_KNOWN_GOOD_SHA"
  printf 'candidate_schema=%s\n' "$candidate_head"
  printf 'last_known_good_schema=%s\n' "$lkg_head"
  printf 'current_dependency_lock_sha256=%s\n' "$(sha256sum requirements-test.lock | awk '{print $1}')"
  printf 'lkg_dependency_lock_sha256=%s\n' "$(sha256sum "$P9D_LKG_SOURCE/requirements-test.lock" | awk '{print $1}')"
} > "$EVIDENCE_DIR/revision-boundary.txt"

bash scripts/ci/bootstrap_cluster_roles.sh
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD '${MIGRATION_PASSWORD}';
CREATE ROLE auth_p5w2_runtime LOGIN PASSWORD '${AUTH_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE app_test_runtime LOGIN PASSWORD '${APP_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE worker_test_runtime LOGIN PASSWORD '${WORKER_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE lifecycle_maintenance_test_runtime LOGIN PASSWORD '${MAINTENANCE_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE finance_config_deployment LOGIN PASSWORD '${FINANCE_CONFIG_RUNTIME_PASSWORD}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT auth_runtime TO auth_p5w2_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO auth_p5w2_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_runtime TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT worker_runtime TO worker_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT lifecycle_maintenance_runtime TO lifecycle_maintenance_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT finance_config_runtime TO finance_config_deployment WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
ALTER ROLE auth_p5w2_runtime SET row_security='on';
ALTER ROLE auth_p5w2_runtime SET statement_timeout='5s';
ALTER ROLE auth_p5w2_runtime SET lock_timeout='2s';
ALTER ROLE auth_p5w2_runtime SET idle_in_transaction_session_timeout='15s';
ALTER ROLE app_test_runtime SET row_security='on';
ALTER ROLE app_test_runtime SET statement_timeout='10s';
ALTER ROLE app_test_runtime SET lock_timeout='3s';
ALTER ROLE app_test_runtime SET idle_in_transaction_session_timeout='30s';
ALTER ROLE worker_test_runtime SET row_security='on';
ALTER ROLE worker_test_runtime SET statement_timeout='15s';
ALTER ROLE worker_test_runtime SET lock_timeout='2s';
ALTER ROLE worker_test_runtime SET idle_in_transaction_session_timeout='30s';
ALTER ROLE lifecycle_maintenance_test_runtime SET row_security='on';
ALTER ROLE finance_config_deployment SET row_security='on';
CREATE DATABASE ${P9D_DB} OWNER migration_owner;
REVOKE ALL ON DATABASE ${P9D_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${P9D_DB} TO migration_owner,auth_p5w2_runtime,app_test_runtime,worker_test_runtime,lifecycle_maintenance_test_runtime,finance_config_deployment;
SQL
bash scripts/ci/verify_cluster_roles.sh
bash scripts/ci/provision_infrastructure_extensions.sh "$P9D_DB"

echo 'P9D_REAL_POSTGRESQL_16=PASS'
pg_config --version | tee "$EVIDENCE_DIR/postgresql-version.txt"
grep -q 'PostgreSQL 16' "$EVIDENCE_DIR/postgresql-version.txt"
python -s scripts/verify_alembic_graph.py
python -s -m alembic -c alembic.ini upgrade "$P9D_PREDECESSOR" 2>&1 | tee "$EVIDENCE_DIR/upgrade-predecessor.log"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9D_DB" -f scripts/ci/p9m_seed_populated_predecessor.sql | tee "$EVIDENCE_DIR/seed.log"
grep -q 'P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS' "$EVIDENCE_DIR/seed.log"
python -s -m alembic -c alembic.ini upgrade "$P9D_HEAD" 2>&1 | tee "$EVIDENCE_DIR/upgrade-head.log"
python -s -m alembic -c alembic.ini current --check-heads
test "$(sudo -u postgres psql -X -qAt -d "$P9D_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9D_HEAD"
sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P9D_DB" -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/pre-rollback-stable.txt"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9D_DB" -f scripts/ci/p9m_verify_head_capability.sql | tee "$EVIDENCE_DIR/head-capability-before.log"
grep -q 'P9M_EXPECTED_CAPABILITY_DELTA=PASS' "$EVIDENCE_DIR/head-capability-before.log"
P9D_FINANCE_DUMP_RESTRICT_KEY='P9DFinanceStateProof'
sudo -u postgres pg_dump --data-only --schema=finance --no-owner --no-privileges --column-inserts --restrict-key="$P9D_FINANCE_DUMP_RESTRICT_KEY" --dbname="$P9D_DB" > "$EVIDENCE_DIR/finance-before-rollback.sql"

if ! id p9dapp >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin p9dapp
fi
P9D_APP_UID="$(id -u p9dapp)"
export P9D_APP_UID
sudo rm -rf "$P9D_LKG_RUNTIME" "$P9D_CANDIDATE_RUNTIME" "$P9D_LKG_WORK" "$P9D_CANDIDATE_WORK" "$P9D_HOME"
sudo install -d -m 0750 -o p9dapp -g p9dapp "$P9D_LKG_RUNTIME" "$P9D_CANDIDATE_RUNTIME" "$P9D_LKG_WORK" "$P9D_CANDIDATE_WORK" "$P9D_HOME"
sudo cp -a "$P9D_LKG_SOURCE/app" "$P9D_LKG_SOURCE/security" "$P9D_LKG_RUNTIME/"
sudo cp -a "$GITHUB_WORKSPACE/app" "$GITHUB_WORKSPACE/security" "$P9D_CANDIDATE_RUNTIME/"
sudo chown -R p9dapp:p9dapp "$P9D_LKG_RUNTIME" "$P9D_CANDIDATE_RUNTIME"
sudo chmod -R u=rwX,g=rX,o= "$P9D_LKG_RUNTIME" "$P9D_CANDIDATE_RUNTIME"

P9D_NGINX_DIR=/tmp/p9d-nginx
P9D_NGINX_CONF="$P9D_NGINX_DIR/nginx.conf"
P9D_NGINX_PID="$P9D_NGINX_DIR/nginx.pid"
sudo rm -rf "$P9D_NGINX_DIR"
sudo mkdir -p "$P9D_NGINX_DIR"

cleanup() {
  sudo pkill -u p9dapp -f 'uvicorn app.main:app' >/dev/null 2>&1 || true
  if [ -s /tmp/p9d-metrics.pid ]; then kill "$(cat /tmp/p9d-metrics.pid)" >/dev/null 2>&1 || true; fi
  if [ -s "$P9D_NGINX_PID" ]; then sudo nginx -p "$P9D_NGINX_DIR/" -c "$P9D_NGINX_CONF" -s quit >/dev/null 2>&1 || true; fi
  sudo iptables -D OUTPUT -m owner --uid-owner "$P9D_APP_UID" ! -d 127.0.0.0/8 -j REJECT >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_live() {
  local port="$1" output="$2" log="$3" ok=0
  for _ in $(seq 1 120); do
    if curl --fail --silent "http://127.0.0.1:${port}/_system/live" > "$output"; then ok=1; break; fi
    sleep 0.5
  done
  if [ "$ok" -ne 1 ]; then cat "$log" >&2 || true; return 1; fi
  python - "$output" <<'PY'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()) == {"status": "alive"}
PY
}

wait_ready() {
  local port="$1" output="$2" log="$3" ok=0
  for _ in $(seq 1 120); do
    if curl --fail --silent "http://127.0.0.1:${port}/_system/ready" > "$output"; then ok=1; break; fi
    sleep 0.5
  done
  if [ "$ok" -ne 1 ]; then cat "$log" >&2 || true; return 1; fi
  python - "$output" <<'PY'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()) == {"status": "ready"}
PY
}

wait_not_ready() {
  local port="$1" output="$2" log="$3" status=""
  for _ in $(seq 1 40); do
    status="$(curl --silent --output "$output" --write-out '%{http_code}' "http://127.0.0.1:${port}/_system/ready" || true)"
    if [ "$status" = '503' ] && grep -q 'dependencies_unavailable' "$output"; then
      printf '%s\n' "$status"
      return 0
    fi
    sleep 0.25
  done
  echo "P9-D router did not converge to the bad candidate readiness failure; last_status=${status}" >&2
  cat "$output" >&2 || true
  cat "$log" >&2 || true
  return 1
}

launch_app() {
  local runtime="$1" work="$2" port="$3" redis_url="$4" broker_url="$5" result_url="$6" log="$7" pidfile="$8" python_bin
  if [[ "$runtime" == "$P9D_LKG_RUNTIME" ]]; then
    python_bin="$P9D_LKG_VENV/bin/python"
  else
    python_bin="$(command -v python)"
  fi
  (
    exec sudo -u p9dapp env -i \
      HOME="$P9D_HOME" PATH="$PATH" PYTHONPATH="$runtime" PYTHONDONTWRITEBYTECODE=1 ENVIRONMENT=test \
      DATABASE_URL="postgresql+asyncpg://${P9D_API_LOGIN}:${APP_RUNTIME_PASSWORD}@127.0.0.1:5432/${P9D_DB}" \
      REDIS_URL="$redis_url" \
      CELERY_BROKER_URL="$broker_url" \
      CELERY_RESULT_BACKEND="$result_url" \
      SECRET_KEY=p9d-synthetic-ci-secret AWS_ACCESS_KEY_ID=p9d-synthetic AWS_SECRET_ACCESS_KEY=p9d-synthetic S3_BUCKET_NAME=p9d-synthetic \
      P8_METRICS_OTLP_ENDPOINT="http://127.0.0.1:${P9D_METRICS_PORT}/v1/metrics" P8_METRICS_EXPORT_INTERVAL_SECONDS=1 P8_METRICS_EXPORT_TIMEOUT_SECONDS=2 \
      NOTIFICATION_EMAIL_PROVIDER_MODE=disabled SEARCH_PROVIDER_MODE=disabled PLATFORM_BILLING_PROVIDER_MODE=disabled \
      PLATFORM_BILLING_CHECKOUT=false PLATFORM_BILLING_WEBHOOK_PROCESSING=false PLATFORM_BILLING_DUNNING_TRANSITIONS=false PLATFORM_BILLING_NOTIFICATIONS=false \
      PYTHON_BIN="$python_bin" APP_PORT="$port" APP_WORK="$work" \
      /bin/bash -c 'cd "$APP_WORK" && exec "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$APP_PORT"'
  ) > "$log" 2>&1 &
  echo "$!" > "$pidfile"
}

write_nginx_target() {
  local target_port="$1"
  sudo tee "$P9D_NGINX_CONF" >/dev/null <<EOF
worker_processes 1;
pid ${P9D_NGINX_PID};
error_log ${P9D_NGINX_DIR}/error.log notice;
events { worker_connections 128; }
http {
  access_log ${P9D_NGINX_DIR}/access.log;
  upstream doers_backend { server 127.0.0.1:${target_port}; }
  server {
    listen 127.0.0.1:${P9D_ROUTER_PORT};
    location / {
      proxy_pass http://doers_backend;
      proxy_connect_timeout 1s;
      proxy_read_timeout 5s;
      proxy_send_timeout 5s;
    }
  }
}
EOF
  if [ -s "$P9D_NGINX_PID" ]; then
    sudo nginx -p "$P9D_NGINX_DIR/" -c "$P9D_NGINX_CONF" -t
    sudo nginx -p "$P9D_NGINX_DIR/" -c "$P9D_NGINX_CONF" -s reload
  else
    sudo nginx -p "$P9D_NGINX_DIR/" -c "$P9D_NGINX_CONF" -t
    sudo nginx -p "$P9D_NGINX_DIR/" -c "$P9D_NGINX_CONF"
  fi
}

python -s scripts/ci/p8o_otlp_collector.py --host 127.0.0.1 --port "$P9D_METRICS_PORT" --capture "$EVIDENCE_DIR/p9d-app-metrics.jsonl" > "$EVIDENCE_DIR/metrics-collector.log" 2>&1 &
echo "$!" > /tmp/p9d-metrics.pid
for _ in $(seq 1 30); do curl --fail --silent "http://127.0.0.1:${P9D_METRICS_PORT}/healthz" >/dev/null && break; sleep 0.2; done
curl --fail --silent "http://127.0.0.1:${P9D_METRICS_PORT}/healthz" > "$EVIDENCE_DIR/metrics-health.txt"

python - <<'PY'
import os
import redis
client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
assert client.ping() is True
PY
echo 'P9D_HEALTHY_REDIS_AUTHENTICATED=PASS'

sudo iptables -I OUTPUT 1 -m owner --uid-owner "$P9D_APP_UID" ! -d 127.0.0.0/8 -j REJECT
if sudo -u p9dapp /usr/bin/curl --fail --silent --show-error --connect-timeout 2 http://1.1.1.1 > "$EVIDENCE_DIR/provider-egress.stdout" 2> "$EVIDENCE_DIR/provider-egress.stderr"; then
  echo 'P9-D runtime identity unexpectedly reached non-loopback network.' >&2
  exit 1
fi
echo 'P9D_LIVE_PROVIDER_EGRESS_BLOCKED=PASS'

launch_app "$P9D_LKG_RUNTIME" "$P9D_LKG_WORK" "$P9D_LKG_PORT" "$REDIS_URL" "$CELERY_BROKER_URL" "$CELERY_RESULT_BACKEND" "$EVIDENCE_DIR/lkg-app.log" /tmp/p9d-lkg.pid
wait_live "$P9D_LKG_PORT" "$EVIDENCE_DIR/lkg-live.json" "$EVIDENCE_DIR/lkg-app.log"
wait_ready "$P9D_LKG_PORT" "$EVIDENCE_DIR/lkg-ready.json" "$EVIDENCE_DIR/lkg-app.log"
write_nginx_target "$P9D_LKG_PORT"
wait_ready "$P9D_ROUTER_PORT" "$EVIDENCE_DIR/router-lkg-ready.json" "$P9D_NGINX_DIR/error.log"
echo 'P9D_LAST_KNOWN_GOOD_TRAFFIC_READY=PASS'

P9D_BAD_REDIS_URL="redis://127.0.0.1:${P9D_BAD_REDIS_PORT}/0"
P9D_BAD_BROKER_URL="redis://127.0.0.1:${P9D_BAD_REDIS_PORT}/1"
P9D_BAD_RESULT_URL="redis://127.0.0.1:${P9D_BAD_REDIS_PORT}/2"
launch_app "$P9D_CANDIDATE_RUNTIME" "$P9D_CANDIDATE_WORK" "$P9D_CANDIDATE_PORT" "$P9D_BAD_REDIS_URL" "$P9D_BAD_BROKER_URL" "$P9D_BAD_RESULT_URL" "$EVIDENCE_DIR/bad-candidate.log" /tmp/p9d-candidate.pid
wait_live "$P9D_CANDIDATE_PORT" "$EVIDENCE_DIR/bad-candidate-live.json" "$EVIDENCE_DIR/bad-candidate.log"
status="$(curl --silent --output "$EVIDENCE_DIR/bad-candidate-ready-body.json" --write-out '%{http_code}' "http://127.0.0.1:${P9D_CANDIDATE_PORT}/_system/ready")"
test "$status" = '503'
grep -q 'dependencies_unavailable' "$EVIDENCE_DIR/bad-candidate-ready-body.json"
echo 'P9D_BAD_RELEASE_INJECTED=PASS'
echo 'P9D_READINESS_FAILURE_DETECTED=PASS'

python -s scripts/ci/p9d_durable_work_harness.py prepare | tee "$EVIDENCE_DIR/durable-prepare.log"
grep -q 'P9D_DURABLE_WORK_CLAIM_SURVIVED_BAD_RELEASE=PASS' "$EVIDENCE_DIR/durable-prepare.log"

write_nginx_target "$P9D_CANDIDATE_PORT"
router_bad_status="$(wait_not_ready "$P9D_ROUTER_PORT" "$EVIDENCE_DIR/candidate-router-ready-body.json" "$P9D_NGINX_DIR/error.log")"
test "$router_bad_status" = '503'
printf '%s\n' "$router_bad_status" > "$EVIDENCE_DIR/candidate-router-status.txt"
grep -q 'dependencies_unavailable' "$EVIDENCE_DIR/candidate-router-ready-body.json"
echo 'P9D_BAD_RELEASE_ROUTER_CONVERGED=PASS'

write_nginx_target "$P9D_LKG_PORT"
wait_ready "$P9D_ROUTER_PORT" "$EVIDENCE_DIR/rollback-router-ready.json" "$P9D_NGINX_DIR/error.log"
echo 'P9D_TRAFFIC_RETURNED_TO_LAST_KNOWN_GOOD=PASS'

sudo pkill -u p9dapp -f "uvicorn app.main:app --host 127.0.0.1 --port ${P9D_CANDIDATE_PORT}" >/dev/null 2>&1 || true
"$P9D_LKG_VENV/bin/python" -s scripts/ci/p9d_durable_work_harness.py recover | tee "$EVIDENCE_DIR/durable-recover.log"
grep -q 'P9D_DURABLE_WORK_RECOVERED_BY_LAST_KNOWN_GOOD=PASS' "$EVIDENCE_DIR/durable-recover.log"
grep -q 'P9D_NO_LOST_DURABLE_WORK=PASS' "$EVIDENCE_DIR/durable-recover.log"
grep -q 'P9D_SINGLE_TERMINAL_EFFECT=PASS' "$EVIDENCE_DIR/durable-recover.log"

wait_ready "$P9D_ROUTER_PORT" "$EVIDENCE_DIR/post-recovery-router-ready.json" "$P9D_NGINX_DIR/error.log"
test "$(sudo -u postgres psql -X -qAt -d "$P9D_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9D_HEAD"
echo 'P9D_DATABASE_REMAINED_AT_HEAD=PASS'

sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P9D_DB" -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/post-rollback-stable.txt"
cmp "$EVIDENCE_DIR/pre-rollback-stable.txt" "$EVIDENCE_DIR/post-rollback-stable.txt"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9D_DB" -f scripts/ci/p9m_verify_head_capability.sql | tee "$EVIDENCE_DIR/head-capability-after.log"
grep -q 'P9M_EXPECTED_CAPABILITY_DELTA=PASS' "$EVIDENCE_DIR/head-capability-after.log"
sudo -u postgres pg_dump --data-only --schema=finance --no-owner --no-privileges --column-inserts --restrict-key="$P9D_FINANCE_DUMP_RESTRICT_KEY" --dbname="$P9D_DB" > "$EVIDENCE_DIR/finance-after-rollback.sql"
cmp "$EVIDENCE_DIR/finance-before-rollback.sql" "$EVIDENCE_DIR/finance-after-rollback.sql"
echo 'P9D_POST_ROLLBACK_INTEGRITY=PASS'
echo 'P9D_NO_FINANCIAL_STATE_CHANGE=PASS'

python -m pytest --noconftest -q --maxfail=1 tests/test_p4d_refund_authority_static_contracts.py
python -m pytest --noconftest -q --maxfail=1 \
  tests/test_p4d_refund_authority_runtime.py::test_concurrent_materialization_creates_exactly_one_logical_command \
  tests/test_p4d_refund_authority_runtime.py::test_same_worker_expired_lease_reclaim_rotates_fence_and_rejects_stale_fence
echo 'P9D_FINANCE_EXACTLY_ONCE_FENCING_REPROVED=PASS'

sha256sum "$EVIDENCE_DIR/pre-rollback-stable.txt" "$EVIDENCE_DIR/post-rollback-stable.txt" > "$EVIDENCE_DIR/integrity-fingerprints.sha256"
sha256sum "$EVIDENCE_DIR/finance-before-rollback.sql" "$EVIDENCE_DIR/finance-after-rollback.sql" > "$EVIDENCE_DIR/finance-fingerprints.sha256"
python - <<'PY'
import hashlib, json, os
from pathlib import Path
p = Path(os.environ['EVIDENCE_DIR'])
def digest(name: str) -> str:
    return hashlib.sha256((p / name).read_bytes()).hexdigest()
recovery = json.loads((p / 'durable-recovery.json').read_text(encoding='utf-8'))
assert recovery['projection_count'] == 1
assert recovery['projection_version'] == 1
assert recovery['duplicate_claimed'] == 0
assert recovery['lease_fence'] == 2
assert (p / 'candidate-router-status.txt').read_text(encoding='utf-8').strip() == '503'
assert json.loads((p / 'rollback-router-ready.json').read_text(encoding='utf-8')) == {'status': 'ready'}
d = {
  'candidate_sha': os.environ['P9D_CANDIDATE_SHA'],
  'last_known_good_sha': os.environ['P9D_LAST_KNOWN_GOOD_SHA'],
  'schema_head': os.environ['P9D_HEAD'],
  'postgresql_major': 16,
  'bad_release_injection': 'candidate_dependency_readiness_failure',
  'candidate_process_live': True,
  'candidate_readiness_failed': True,
  'traffic_observed_bad_release_503': True,
  'traffic_returned_to_last_known_good': True,
  'database_downgrade_used': False,
  'durable_claim_survived_bad_release': True,
  'durable_work_recovered_by_last_known_good': True,
  'durable_terminal_projection_count': recovery['projection_count'],
  'duplicate_durable_claim_after_recovery': recovery['duplicate_claimed'],
  'p9_integrity_fingerprint_before': digest('pre-rollback-stable.txt'),
  'p9_integrity_fingerprint_after': digest('post-rollback-stable.txt'),
  'finance_fingerprint_before': digest('finance-before-rollback.sql'),
  'finance_fingerprint_after': digest('finance-after-rollback.sql'),
  'finance_exactly_once_fencing_reproved': True,
  'live_provider_egress_blocked': True,
  'refund_provider_execution': 'deferred_fail_closed',
  'decision': 'PASS',
}
assert d['p9_integrity_fingerprint_before'] == d['p9_integrity_fingerprint_after']
assert d['finance_fingerprint_before'] == d['finance_fingerprint_after']
(p / 'decision.json').write_text(json.dumps(d, indent=2, sort_keys=True) + '\n', encoding='utf-8')
PY
cat "$EVIDENCE_DIR/decision.json"
cleanup
trap - EXIT

echo 'P9D_RUNTIME_PROOF=PASS'
echo 'P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED'