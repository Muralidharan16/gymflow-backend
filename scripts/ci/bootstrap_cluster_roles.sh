#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Stream the manifest-rendered SQL across the privilege boundary. A runner-owned
# mktemp file is intentionally avoided because sudo -u postgres must never rely
# on broader filesystem permissions just to consume the canonical bootstrap.
python -s scripts/render_cluster_role_bootstrap.py \
  | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres
