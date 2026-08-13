#!/usr/bin/env bash
set -euo pipefail

# Fresh-cluster production/pre-production bootstrap. This command never embeds
# credentials and never repairs an existing cluster. Supply the separately
# controlled postgres administrator connection through normal libpq variables.
PSQL_BIN="${PSQL_BIN:-psql}"
ADMIN_DATABASE="${DOERS_CLUSTER_ADMIN_DATABASE:-postgres}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if ! command -v "$PSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: psql client not found: $PSQL_BIN" >&2
  exit 2
fi

cd "$ROOT"
# Stream create-only SQL rather than materializing a privileged bootstrap file.
# pipefail makes either renderer failure or psql rejection fail the command.
python -s scripts/render_cluster_role_bootstrap.py \
  | "$PSQL_BIN" -X -v ON_ERROR_STOP=1 --dbname="$ADMIN_DATABASE"
