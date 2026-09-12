#!/usr/bin/env bash
set -euo pipefail
: "${MIGRATION_PASSWORD:?MIGRATION_PASSWORD is required}"
export DOERS_CLUSTER_VERIFY_DATABASE_URL="postgresql+psycopg://migration_owner:${MIGRATION_PASSWORD}@127.0.0.1:5432/postgres"
python -s scripts/verify_cluster_role_bootstrap.py
