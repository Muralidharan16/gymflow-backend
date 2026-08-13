#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SQL_FILE="$(mktemp)"
trap 'rm -f "$SQL_FILE"' EXIT

cd "$ROOT"
python -s scripts/render_cluster_role_bootstrap.py > "$SQL_FILE"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -f "$SQL_FILE"
