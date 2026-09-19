#!/usr/bin/env bash
set -euo pipefail

: "${DOERS_PAY2_CLUSTER_ADMIN_DATABASE_URL:?DOERS_PAY2_CLUSTER_ADMIN_DATABASE_URL is required}"
: "${DOERS_PAY2_CLUSTER_ADMIN_PSQL_DSN:?DOERS_PAY2_CLUSTER_ADMIN_PSQL_DSN is required}"
PSQL_BIN="${PSQL_BIN:-psql}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if ! command -v "$PSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: psql client not found: $PSQL_BIN" >&2
  exit 2
fi

python -s scripts/verify_pay2_finance_role_state.py predecessor
python -s scripts/render_pay2_finance_role_expansion.py \
  | "$PSQL_BIN" -X -v ON_ERROR_STOP=1 --dbname="$DOERS_PAY2_CLUSTER_ADMIN_PSQL_DSN"
python -s scripts/verify_pay2_finance_role_state.py full
