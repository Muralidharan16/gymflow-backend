#!/usr/bin/env bash
# UNAPPROVED PRODUCTION TEMPLATE. Separate R19F owner authorization is required.

set -Eeuo pipefail
set +x
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PSQL_BIN="${PSQL_BIN:-psql}"
OWNER_AUTHORIZATION=""
OUTPUT_DIR=""
DATABASE_SCOPE_MAP=""
SOURCE_DATABASE=""
CAPTURE_LABEL=""
VALIDATE_OUTPUT_ONLY=0

usage() {
  printf '%s\n' \
    'UNAPPROVED read-only template.' \
    'Required future arguments:' \
    '  --owner-authorization R19F_PEER_ADMIN_CAPTURE_AUTHORIZED' \
    '  --source-database SEMANTIC_NAME' \
    '  --label SAFE_LABEL' \
    'Optional:' \
    '  --database-scope-map FILE' \
    '  --output-dir SECURE_DIRECTORY' \
    '  --validate-output-dir-only' \
    '  --validate-template'
}

canonicalize_output_dir() {
  "$PYTHON_BIN" - "$REPOSITORY_ROOT" "$1" <<'PY'
import sys
from pathlib import Path

try:
    repository = Path(sys.argv[1]).resolve(strict=True)
    destination = Path(sys.argv[2]).expanduser().resolve(strict=False)
except (OSError, RuntimeError, ValueError):
    raise SystemExit(2)

if destination == repository or repository in destination.parents:
    raise SystemExit(3)

print(destination)
PY
}

if [[ "${1:-}" == "--validate-template" ]]; then
  printf '%s\n' 'template_validation=offline_only'
  exit 0
fi

while (($#)); do
  case "$1" in
    --owner-authorization) OWNER_AUTHORIZATION="${2:-}"; shift 2 ;;
    --source-database) SOURCE_DATABASE="${2:-}"; shift 2 ;;
    --label) CAPTURE_LABEL="${2:-}"; shift 2 ;;
    --database-scope-map) DATABASE_SCOPE_MAP="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --validate-output-dir-only) VALIDATE_OUTPUT_ONLY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

OUTPUT_REAL=""
if [[ -n "$OUTPUT_DIR" ]]; then
  if ! OUTPUT_REAL="$(canonicalize_output_dir "$OUTPUT_DIR")"; then
    printf '%s\n' 'capture_blocked=unsafe_output_directory' >&2
    exit 3
  fi
elif ((VALIDATE_OUTPUT_ONLY)); then
  printf '%s\n' 'capture_blocked=output_directory_required' >&2
  exit 3
fi

if ((VALIDATE_OUTPUT_ONLY)); then
  printf '%s\n' 'capture_output_path_valid=yes'
  exit 0
fi

if [[ "$OWNER_AUTHORIZATION" != "R19F_PEER_ADMIN_CAPTURE_AUTHORIZED" ]]; then
  printf '%s\n' 'capture_blocked=separate_owner_authorization_required' >&2
  exit 3
fi
if [[ ! "$SOURCE_DATABASE" =~ ^[a-z][a-z0-9_-]{0,62}$ ]]; then
  printf '%s\n' 'capture_blocked=invalid_semantic_source_database' >&2
  exit 3
fi
if [[ -z "$CAPTURE_LABEL" || ${#CAPTURE_LABEL} -gt 160 ]]; then
  printf '%s\n' 'capture_blocked=invalid_label' >&2
  exit 3
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/doers-role-capture-v2.XXXXXX")"
chmod 700 "$TEMP_DIR"
cleanup() {
  find "$TEMP_DIR" -type f -exec chmod 600 {} + 2>/dev/null || true
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

if [[ -n "$OUTPUT_REAL" ]]; then
  mkdir -p -- "$OUTPUT_REAL"
  chmod 700 "$OUTPUT_REAL"
  OUTPUT_DIR="$OUTPUT_REAL"
fi

if [[ -z "$DATABASE_SCOPE_MAP" ]]; then
  DATABASE_SCOPE_MAP="$TEMP_DIR/database-scope-map.json"
  printf '%s\n' '{}' > "$DATABASE_SCOPE_MAP"
fi

read -r -d '' CATALOG_SQL <<'SQL' || true
WITH managed(role_name) AS (
    VALUES
      ('app_migrator'),
      ('app_rls_executor'),
      ('app_runtime'),
      ('app_security_owner'),
      ('app_user'),
      ('audit_writer'),
      ('branch_admin'),
      ('branch_viewer'),
      ('internal_billing_worker'),
      ('migration_owner'),
      ('ops_support'),
      ('readonly_analytics'),
      ('test_runner')
), role_rows AS (
    SELECT jsonb_build_object(
        'role_name', managed.role_name,
        'exists', role_data.rolname IS NOT NULL,
        'superuser', role_data.rolsuper,
        'inherit', role_data.rolinherit,
        'create_role', role_data.rolcreaterole,
        'create_database', role_data.rolcreatedb,
        'login', role_data.rolcanlogin,
        'replication', role_data.rolreplication,
        'bypass_rls', role_data.rolbypassrls,
        'connection_limit', role_data.rolconnlimit,
        'valid_until', CASE
            WHEN role_data.rolname IS NULL THEN NULL
            WHEN role_data.rolvaliduntil IS NULL THEN 'infinity'
            ELSE role_data.rolvaliduntil::text
        END,
        'password_classification', CASE
            WHEN role_data.rolname IS NULL THEN NULL
            WHEN role_data.rolpassword IS NULL THEN 'none'
            WHEN split_part(role_data.rolpassword, '$', 1) = 'SCRAM-SHA-256' THEN 'scram-sha-256'
            WHEN left(role_data.rolpassword, 3) = 'md5' THEN 'md5'
            ELSE 'unrecognized'
        END,
        'comment', pg_catalog.shobj_description(role_data.oid, 'pg_authid')
    ) AS value
    FROM managed
    LEFT JOIN pg_catalog.pg_authid AS role_data ON role_data.rolname = managed.role_name
), membership_rows AS (
    SELECT jsonb_build_object(
        'granted_role', granted_role.rolname,
        'member', member_role.rolname,
        'grantor', grantor_role.rolname,
        'admin_option', membership.admin_option,
        'inherit_option', membership.inherit_option,
        'set_option', membership.set_option
    ) AS value
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
    JOIN pg_catalog.pg_roles AS grantor_role ON grantor_role.oid = membership.grantor
    WHERE granted_role.rolname IN (SELECT role_name FROM managed)
       OR member_role.rolname IN (SELECT role_name FROM managed)
), setting_rows AS (
    SELECT jsonb_build_object(
        'role_name', role_data.rolname,
        'database_name', CASE WHEN setting.setdatabase = 0 THEN NULL ELSE database_data.datname END,
        'setting_name', split_part(setting_value, '=', 1),
        'setting_value', substring(setting_value FROM position('=' IN setting_value) + 1)
    ) AS value
    FROM pg_catalog.pg_db_role_setting AS setting
    JOIN pg_catalog.pg_roles AS role_data ON role_data.oid = setting.setrole
    LEFT JOIN pg_catalog.pg_database AS database_data ON database_data.oid = setting.setdatabase
    CROSS JOIN LATERAL unnest(setting.setconfig) AS setting_value
    WHERE role_data.rolname IN (SELECT role_name FROM managed)
)
SELECT jsonb_build_object(
    'postgresql_major_version', current_setting('server_version_num')::integer / 10000,
    'roles', (SELECT jsonb_agg(value ORDER BY value->>'role_name') FROM role_rows),
    'memberships', COALESCE((SELECT jsonb_agg(value ORDER BY value->>'granted_role', value->>'member') FROM membership_rows), '[]'::jsonb),
    'role_settings', COALESCE((SELECT jsonb_agg(value ORDER BY value->>'role_name', value->>'database_name', value->>'setting_name') FROM setting_rows), '[]'::jsonb)
)::text;
SQL

for capture_number in 1 2; do
  catalog_file="$TEMP_DIR/catalog-${capture_number}.json"
  metadata_file="$TEMP_DIR/metadata-${capture_number}.json"
  manifest_file="$TEMP_DIR/capture-${capture_number}.current-evidence.json"

  "$PSQL_BIN" \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --tuples-only \
    --no-align \
    --quiet \
    --command "$CATALOG_SQL" > "$catalog_file"
  chmod 600 "$catalog_file"

  "$PYTHON_BIN" -c \
    'import json,sys; json.dump({"captured_at":sys.argv[1],"execution_mode":"peer_admin_read_only","source_database":sys.argv[2],"label":sys.argv[3]},open(sys.argv[4],"w",encoding="utf-8"),sort_keys=True,separators=(",",":"))' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SOURCE_DATABASE" "$CAPTURE_LABEL" "$metadata_file"
  chmod 600 "$metadata_file"

  "$PYTHON_BIN" "$SCRIPT_DIR/generator.py" \
    --catalog-json "$catalog_file" \
    --managed-roles "$SCRIPT_DIR/managed_roles.json" \
    --metadata-json "$metadata_file" \
    --database-scope-map "$DATABASE_SCOPE_MAP" \
    --output "$manifest_file"
  "$PYTHON_BIN" "$SCRIPT_DIR/validate_manifest.py" "$manifest_file"
done

"$PYTHON_BIN" "$SCRIPT_DIR/compare_manifests.py" \
  "$TEMP_DIR/capture-1.current-evidence.json" \
  "$TEMP_DIR/capture-2.current-evidence.json" \
  --format human

if [[ -n "$OUTPUT_DIR" ]]; then
  install -m 600 "$TEMP_DIR/capture-1.current-evidence.json" "$OUTPUT_DIR/capture-1.current-evidence.json"
  install -m 600 "$TEMP_DIR/capture-2.current-evidence.json" "$OUTPUT_DIR/capture-2.current-evidence.json"
  printf '%s\n' 'evidence_retained=yes'
else
  printf '%s\n' 'evidence_retained=no'
fi
printf '%s\n' 'baseline_approved=no'
