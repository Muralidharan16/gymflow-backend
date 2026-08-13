#!/usr/bin/env bash
set -euo pipefail

# Fresh-cluster production/pre-production bootstrap. This command never embeds
# credentials and never repairs an existing cluster. Supply the separately
# controlled postgres administrator connection through normal libpq variables.
PSQL_BIN="${PSQL_BIN:-psql}"
ADMIN_DATABASE="${DOERS_CLUSTER_ADMIN_DATABASE:-postgres}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SQL_FILE="$(mktemp)"
trap 'rm -f "$SQL_FILE"' EXIT

if ! command -v "$PSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: psql client not found: $PSQL_BIN" >&2
  exit 2
fi

cd "$ROOT"
python -s scripts/render_cluster_role_bootstrap.py > "$SQL_FILE"
"$PSQL_BIN" -X -v ON_ERROR_STOP=1 --dbname="$ADMIN_DATABASE" -f "$SQL_FILE"
