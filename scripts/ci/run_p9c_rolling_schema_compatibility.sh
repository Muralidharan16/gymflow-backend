#!/usr/bin/env bash
set -euo pipefail

: "${P9C_OLD_APP_SHA:?}"
: "${P9C_PREDECESSOR:?}"
: "${P9C_HEAD:?}"
: "${P9C_DB:?}"
: "${P9C_CANDIDATE_SHA:?}"
: "${P9C_OLD_SOURCE:?}"
: "${P9C_OLD_RUNTIME:?}"
: "${P9C_NEW_RUNTIME:?}"
: "${P9C_OLD_WORK:?}"
: "${P9C_NEW_WORK:?}"
: "${P9C_HOME:?}"
: "${P9C_REDIS_PORT:?}"
: "${P9C_METRICS_PORT:?}"
: "${P9C_OLD_PORT:?}"
: "${P9C_NEW_PORT:?}"
: "${P9C_API_LOGIN:?}"
: "${EVIDENCE_DIR:?}"
: "${GITHUB_WORKSPACE:?}"

mkdir -p "$EVIDENCE_DIR"
test "$(git rev-parse HEAD)" = "$P9C_CANDIDATE_SHA"
git merge-base --is-ancestor "$P9C_OLD_APP_SHA" HEAD

rm -rf "$P9C_OLD_SOURCE"
git worktree add --detach "$P9C_OLD_SOURCE" "$P9C_OLD_APP_SHA"
old_head="$(cd "$P9C_OLD_SOURCE" && python -s -m alembic -c alembic.ini heads | awk '{print $1}')"
new_head="$(python -s -m alembic -c alembic.ini heads | awk '{print $1}')"
test "$old_head" = "$P9C_PREDECESSOR"
test "$new_head" = "$P9C_HEAD"

P9C_OLD_VENV=/tmp/p9c-old-venv
rm -rf "$P9C_OLD_VENV"
python -m venv "$P9C_OLD_VENV"
"$P9C_OLD_VENV/bin/python" -m pip install 'pip==26.2.1'
"$P9C_OLD_VENV/bin/python" -m pip install -r "$P9C_OLD_SOURCE/requirements-test.lock"
"$P9C_OLD_VENV/bin/python" -m pip check
"$P9C_OLD_VENV/bin/python" -m pip freeze | LC_ALL=C sort > "$EVIDENCE_DIR/old-runtime-resolved.txt"
diff -u "$P9C_OLD_SOURCE/requirements-test.lock" "$EVIDENCE_DIR/old-runtime-resolved.txt"
echo 'P9C_HISTORICAL_DEPENDENCY_RUNTIME=PASS'

migration_delta="$(git diff --name-only "${P9C_OLD_APP_SHA}..${P9C_CANDIDATE_SHA}" -- alembic/versions/)"
test "$migration_delta" = 'alembic/versions/zk07d8e9f0a45_p8_lifecycle_dead_letter_snapshot.py'
if grep -Eiq '\b(drop_table|drop_column|alter_column)\b|DROP[[:space:]]+(TABLE|COLUMN)' \
    alembic/versions/zk07d8e9f0a45_p8_lifecycle_dead_letter_snapshot.py; then
  echo 'P9-C destructive contract operation detected in overlap migration.' >&2
  exit 1
fi
{
  printf 'old_application_sha=%s\n' "$P9C_OLD_APP_SHA"
  printf 'old_native_schema=%s\n' "$old_head"
  printf 'new_application_sha=%s\n' "$P9C_CANDIDATE_SHA"
  printf 'new_native_schema=%s\n' "$new_head"
  printf 'current_dependency_lock_sha256=%s\n' "$(sha256sum requirements-test.lock | awk '{print $1}')"
  printf 'old_dependency_lock_sha256=%s\n' "$(sha256sum "$P9C_OLD_SOURCE/requirements-test.lock" | awk '{print $1}')"
  printf 'migration_delta=%s\n' "$migration_delta"
} > "$EVIDENCE_DIR/revision-boundary.txt"
echo 'P9C_EXPAND_MIGRATE_CONTRACT_BOUNDARY=PASS'

bash scripts/ci/bootstrap_cluster_roles.sh
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres <<SQL
ALTER ROLE migration_owner PASSWORD 'ci-p9c-migration-owner';
CREATE ROLE app_test_runtime LOGIN PASSWORD 'ci-p9c-app-runtime'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE app_test_runtime SET row_security = 'on';
ALTER ROLE app_test_runtime SET statement_timeout = '5s';
ALTER ROLE app_test_runtime SET lock_timeout = '2s';
ALTER ROLE app_test_runtime SET idle_in_transaction_session_timeout = '15s';
GRANT app_runtime TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
GRANT app_user TO app_test_runtime WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;
CREATE DATABASE ${P9C_DB} OWNER migration_owner;
GRANT CONNECT ON DATABASE ${P9C_DB} TO app_test_runtime;
SQL
bash scripts/ci/verify_cluster_roles.sh

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9C_DB" <<'SQL'
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

python -s -m alembic -c alembic.ini upgrade "$P9C_PREDECESSOR" 2>&1 | tee "$EVIDENCE_DIR/upgrade-to-predecessor.log"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_seed_populated_predecessor.sql | tee "$EVIDENCE_DIR/seed.log"
grep -q 'P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS' "$EVIDENCE_DIR/seed.log"
sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/pre-upgrade-stable.txt"

python -s -m alembic -c alembic.ini upgrade "$P9C_HEAD" 2>&1 | tee "$EVIDENCE_DIR/upgrade-to-head.log"
test "$(sudo -u postgres psql -X -qAt -d "$P9C_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9C_HEAD"
sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/post-upgrade-stable.txt"
cmp "$EVIDENCE_DIR/pre-upgrade-stable.txt" "$EVIDENCE_DIR/post-upgrade-stable.txt"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_verify_head_capability.sql | tee "$EVIDENCE_DIR/head-capability-before-overlap.log"
grep -q 'P9M_EXPECTED_CAPABILITY_DELTA=PASS' "$EVIDENCE_DIR/head-capability-before-overlap.log"
echo 'P9C_FORWARD_ONLY_SCHEMA_UPGRADE=PASS'

if ! id p9capp >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin p9capp
fi
P9C_APP_UID="$(id -u p9capp)"
export P9C_APP_UID
printf '%s\n' "$P9C_APP_UID" > "$EVIDENCE_DIR/p9c-app-uid.txt"

sudo rm -rf "$P9C_OLD_RUNTIME" "$P9C_NEW_RUNTIME" "$P9C_OLD_WORK" "$P9C_NEW_WORK" "$P9C_HOME"
sudo install -d -m 0750 -o p9capp -g p9capp "$P9C_OLD_RUNTIME" "$P9C_NEW_RUNTIME" "$P9C_OLD_WORK" "$P9C_NEW_WORK" "$P9C_HOME"
sudo cp -a "$P9C_OLD_SOURCE/app" "$P9C_OLD_SOURCE/security" "$P9C_OLD_RUNTIME/"
sudo cp -a "$GITHUB_WORKSPACE/app" "$GITHUB_WORKSPACE/security" "$P9C_NEW_RUNTIME/"
sudo chown -R p9capp:p9capp "$P9C_OLD_RUNTIME" "$P9C_NEW_RUNTIME"
sudo chmod -R u=rwX,g=rX,o= "$P9C_OLD_RUNTIME" "$P9C_NEW_RUNTIME"
sudo -u p9capp test -r "$P9C_OLD_RUNTIME/app/main.py"
sudo -u p9capp test -r "$P9C_NEW_RUNTIME/app/main.py"
sudo -u p9capp test -r "$P9C_OLD_RUNTIME/security/runtime_identity/process_profiles.v1.json"
sudo -u p9capp test -r "$P9C_NEW_RUNTIME/security/runtime_identity/process_profiles.v1.json"

sudo -u postgres psql -X -qAt -F '|' -v ON_ERROR_STOP=1 -d postgres \
  -c "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = '${P9C_API_LOGIN}'" \
  > "$EVIDENCE_DIR/runtime-role-attributes.txt"
grep -qx 't|f|f|f|f|f' "$EVIDENCE_DIR/runtime-role-attributes.txt"
PGPASSWORD=ci-p9c-app-runtime psql -X -qAt -F '|' -v ON_ERROR_STOP=1 \
  -h 127.0.0.1 -p 5432 -U "$P9C_API_LOGIN" -d "$P9C_DB" \
  -c "SELECT session_user, current_user, current_setting('row_security'), current_setting('statement_timeout'), current_setting('lock_timeout'), current_setting('idle_in_transaction_session_timeout'), pg_has_role(current_user, 'app_runtime', 'USAGE'), pg_has_role(current_user, 'app_user', 'USAGE'), pg_has_role(current_user, 'app_runtime', 'SET'), pg_has_role(current_user, 'app_user', 'SET'), 1" \
  > "$EVIDENCE_DIR/runtime-login-preflight.txt"
grep -qx 'app_test_runtime|app_test_runtime|on|5s|2s|15s|t|t|f|f|1' "$EVIDENCE_DIR/runtime-login-preflight.txt"
echo 'P9C_RUNTIME_LEAST_PRIVILEGE=PASS'

cleanup() {
  sudo pkill -u p9capp -f 'uvicorn app.main:app' >/dev/null 2>&1 || true
  if [ -s /tmp/p9c-metrics.pid ]; then kill "$(cat /tmp/p9c-metrics.pid)" >/dev/null 2>&1 || true; fi
  if [ -s /tmp/p9c-redis.pid ]; then kill "$(cat /tmp/p9c-redis.pid)" >/dev/null 2>&1 || true; fi
  sudo iptables -D OUTPUT -m owner --uid-owner "$P9C_APP_UID" ! -d 127.0.0.0/8 -j REJECT >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_ready() {
  local port="$1" output="$2" log="$3" ready=0
  for _ in $(seq 1 120); do
    if curl --fail --silent "http://127.0.0.1:${port}/_system/ready" > "$output"; then ready=1; break; fi
    sleep 0.5
  done
  if [ "$ready" -ne 1 ]; then
    curl --silent --show-error "http://127.0.0.1:${port}/_system/ready" > "${output}.failure" || true
    cat "${output}.failure" >&2 || true
    cat "$log" >&2 || true
    return 1
  fi
  python - "$output" <<'PY'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()) == {"status": "ready"}
PY
}

launch_app() {
  local runtime="$1" work="$2" port="$3" log="$4" pidfile="$5" python_bin
  if [[ "$runtime" == "$P9C_OLD_RUNTIME" ]]; then
    python_bin="$P9C_OLD_VENV/bin/python"
  else
    python_bin="$(command -v python)"
  fi
  (
    exec sudo -u p9capp env -i \
      HOME="$P9C_HOME" PATH="$PATH" PYTHONPATH="$runtime" PYTHONDONTWRITEBYTECODE=1 ENVIRONMENT=test \
      DATABASE_URL="postgresql+asyncpg://${P9C_API_LOGIN}:ci-p9c-app-runtime@127.0.0.1:5432/${P9C_DB}" \
      REDIS_URL="redis://127.0.0.1:${P9C_REDIS_PORT}/0" \
      CELERY_BROKER_URL="redis://127.0.0.1:${P9C_REDIS_PORT}/1" \
      CELERY_RESULT_BACKEND="redis://127.0.0.1:${P9C_REDIS_PORT}/2" \
      SECRET_KEY=p9c-synthetic-ci-secret AWS_ACCESS_KEY_ID=p9c-synthetic AWS_SECRET_ACCESS_KEY=p9c-synthetic S3_BUCKET_NAME=p9c-synthetic \
      P8_METRICS_OTLP_ENDPOINT="http://127.0.0.1:${P9C_METRICS_PORT}/v1/metrics" P8_METRICS_EXPORT_INTERVAL_SECONDS=1 P8_METRICS_EXPORT_TIMEOUT_SECONDS=2 \
      NOTIFICATION_EMAIL_PROVIDER_MODE=disabled SEARCH_PROVIDER_MODE=disabled PLATFORM_BILLING_PROVIDER_MODE=disabled \
      PLATFORM_BILLING_CHECKOUT=false PLATFORM_BILLING_WEBHOOK_PROCESSING=false PLATFORM_BILLING_DUNNING_TRANSITIONS=false PLATFORM_BILLING_NOTIFICATIONS=false \
      PYTHON_BIN="$python_bin" APP_PORT="$port" APP_WORK="$work" \
      /bin/bash -c 'cd "$APP_WORK" && exec "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$APP_PORT"'
  ) > "$log" 2>&1 &
  echo "$!" > "$pidfile"
}

redis-server --port "$P9C_REDIS_PORT" --bind 127.0.0.1 --save '' --appendonly no --daemonize yes --pidfile /tmp/p9c-redis.pid
redis-cli -h 127.0.0.1 -p "$P9C_REDIS_PORT" ping | tee "$EVIDENCE_DIR/redis-ready.txt"
grep -qx 'PONG' "$EVIDENCE_DIR/redis-ready.txt"
python -s scripts/ci/p8o_otlp_collector.py --host 127.0.0.1 --port "$P9C_METRICS_PORT" --capture "$EVIDENCE_DIR/p9c-app-metrics.jsonl" > "$EVIDENCE_DIR/metrics-collector.log" 2>&1 &
echo "$!" > /tmp/p9c-metrics.pid
for _ in $(seq 1 30); do curl --fail --silent "http://127.0.0.1:${P9C_METRICS_PORT}/healthz" >/dev/null && break; sleep 0.2; done
curl --fail --silent "http://127.0.0.1:${P9C_METRICS_PORT}/healthz" > "$EVIDENCE_DIR/metrics-health.txt"

sudo iptables -I OUTPUT 1 -m owner --uid-owner "$P9C_APP_UID" ! -d 127.0.0.0/8 -j REJECT
if sudo -u p9capp /usr/bin/curl --fail --silent --show-error --connect-timeout 2 http://1.1.1.1 > "$EVIDENCE_DIR/provider-egress.stdout" 2> "$EVIDENCE_DIR/provider-egress.stderr"; then
  echo 'P9-C runtime identity unexpectedly reached non-loopback network.' >&2
  exit 1
fi
echo 'P9C_LIVE_PROVIDER_EGRESS_BLOCKED=PASS'

test "$(sudo -u postgres psql -X -qAt -d "$P9C_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9C_HEAD"
launch_app "$P9C_OLD_RUNTIME" "$P9C_OLD_WORK" "$P9C_OLD_PORT" "$EVIDENCE_DIR/old-app-initial.log" /tmp/p9c-old.pid
wait_ready "$P9C_OLD_PORT" "$EVIDENCE_DIR/old-app-on-upgraded-schema.json" "$EVIDENCE_DIR/old-app-initial.log"
echo 'P9C_OLD_APPLICATION_ON_UPGRADED_SCHEMA=PASS'

launch_app "$P9C_NEW_RUNTIME" "$P9C_NEW_WORK" "$P9C_NEW_PORT" "$EVIDENCE_DIR/new-app-overlap.log" /tmp/p9c-new.pid
wait_ready "$P9C_NEW_PORT" "$EVIDENCE_DIR/new-app-on-upgraded-schema.json" "$EVIDENCE_DIR/new-app-overlap.log"
echo 'P9C_NEW_APPLICATION_ON_UPGRADED_SCHEMA=PASS'

: > "$EVIDENCE_DIR/overlap-probes.txt"
for probe in $(seq 1 20); do
  kill -0 "$(cat /tmp/p9c-old.pid)"
  kill -0 "$(cat /tmp/p9c-new.pid)"
  curl --fail --silent "http://127.0.0.1:${P9C_OLD_PORT}/_system/ready" >/dev/null
  curl --fail --silent "http://127.0.0.1:${P9C_NEW_PORT}/_system/ready" >/dev/null
  printf 'probe=%02d old=ready new=ready\n' "$probe" >> "$EVIDENCE_DIR/overlap-probes.txt"
  sleep 0.1
done
test "$(wc -l < "$EVIDENCE_DIR/overlap-probes.txt")" -eq 20
test "$(sudo -u postgres psql -X -qAt -d "$P9C_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9C_HEAD"
echo 'P9C_OLD_NEW_APPLICATION_OVERLAP=PASS'

sudo pkill -u p9capp -f "uvicorn app.main:app --host 127.0.0.1 --port ${P9C_NEW_PORT}" >/dev/null 2>&1 || true
for _ in $(seq 1 30); do ! curl --fail --silent "http://127.0.0.1:${P9C_NEW_PORT}/_system/ready" >/dev/null 2>&1 && break; sleep 0.2; done
if curl --fail --silent "http://127.0.0.1:${P9C_NEW_PORT}/_system/ready" >/dev/null 2>&1; then echo 'P9-C new app failed to drain.' >&2; exit 1; fi
wait_ready "$P9C_OLD_PORT" "$EVIDENCE_DIR/old-app-after-new-drain.json" "$EVIDENCE_DIR/old-app-initial.log"

sudo pkill -u p9capp -f "uvicorn app.main:app --host 127.0.0.1 --port ${P9C_OLD_PORT}" >/dev/null 2>&1 || true
for _ in $(seq 1 30); do ! curl --fail --silent "http://127.0.0.1:${P9C_OLD_PORT}/_system/ready" >/dev/null 2>&1 && break; sleep 0.2; done
if curl --fail --silent "http://127.0.0.1:${P9C_OLD_PORT}/_system/ready" >/dev/null 2>&1; then echo 'P9-C old app failed to stop.' >&2; exit 1; fi
launch_app "$P9C_OLD_RUNTIME" "$P9C_OLD_WORK" "$P9C_OLD_PORT" "$EVIDENCE_DIR/old-app-rollback-restart.log" /tmp/p9c-old-rollback.pid
wait_ready "$P9C_OLD_PORT" "$EVIDENCE_DIR/old-app-rollback-ready.json" "$EVIDENCE_DIR/old-app-rollback-restart.log"
test "$(sudo -u postgres psql -X -qAt -d "$P9C_DB" -c 'SELECT version_num FROM alembic_version')" = "$P9C_HEAD"
echo 'P9C_APPLICATION_ROLLBACK_ON_UPGRADED_SCHEMA=PASS'

sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_stable_snapshot.sql > "$EVIDENCE_DIR/post-rollback-stable.txt"
cmp "$EVIDENCE_DIR/post-upgrade-stable.txt" "$EVIDENCE_DIR/post-rollback-stable.txt"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d "$P9C_DB" -f scripts/ci/p9m_verify_head_capability.sql | tee "$EVIDENCE_DIR/head-capability-after-rollback.log"
grep -q 'P9M_EXPECTED_CAPABILITY_DELTA=PASS' "$EVIDENCE_DIR/head-capability-after-rollback.log"
echo 'P9C_POST_ROLLBACK_DATA_SECURITY_INTEGRITY=PASS'

sha256sum "$EVIDENCE_DIR/pre-upgrade-stable.txt" "$EVIDENCE_DIR/post-upgrade-stable.txt" "$EVIDENCE_DIR/post-rollback-stable.txt" > "$EVIDENCE_DIR/stable-fingerprints.sha256"
python - <<'PY'
import hashlib, json, os
from pathlib import Path
p = Path(os.environ['EVIDENCE_DIR'])
def digest(name): return hashlib.sha256((p / name).read_bytes()).hexdigest()
d = {
  'candidate_sha': os.environ['P9C_CANDIDATE_SHA'],
  'old_application_sha': os.environ['P9C_OLD_APP_SHA'],
  'old_native_schema': os.environ['P9C_PREDECESSOR'],
  'upgraded_schema': os.environ['P9C_HEAD'],
  'postgresql_major': 16,
  'old_application_on_upgraded_schema': True,
  'new_application_on_upgraded_schema': True,
  'old_new_overlap_probe_count': 20,
  'application_rollback_on_upgraded_schema': True,
  'database_downgrade_used': False,
  'live_provider_egress_blocked': True,
  'stable_fingerprint_pre_upgrade': digest('pre-upgrade-stable.txt'),
  'stable_fingerprint_post_upgrade': digest('post-upgrade-stable.txt'),
  'stable_fingerprint_post_rollback': digest('post-rollback-stable.txt'),
  'refund_provider_execution': 'deferred_fail_closed',
  'decision': 'PASS',
}
assert len({d['stable_fingerprint_pre_upgrade'], d['stable_fingerprint_post_upgrade'], d['stable_fingerprint_post_rollback']}) == 1
(p / 'decision.json').write_text(json.dumps(d, indent=2, sort_keys=True) + '\n', encoding='utf-8')
PY
cat "$EVIDENCE_DIR/decision.json"
cleanup
trap - EXIT

echo 'P9C_RUNTIME_PROOF=PASS'
