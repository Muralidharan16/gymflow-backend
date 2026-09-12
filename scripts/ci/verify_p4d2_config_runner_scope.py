#!/usr/bin/env python3
"""Static scope guard for generated P4D-2 config proof runners."""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOPOLOGY_VARS = {"PGPORT_CHOSEN", "PROOF_ROOT", "TEST_DB"}
SECRET_OR_URL_VARS = {
    "MIGRATION_PASSWORD_VALUE",
    "APP_RUNTIME_PASSWORD_VALUE",
    "WORKER_RUNTIME_PASSWORD_VALUE",
    "MAINTENANCE_RUNTIME_PASSWORD_VALUE",
    "FINANCE_CONFIG_RUNTIME_PASSWORD_VALUE",
    "FINANCE_CONFIG_DATABASE_URL",
}
REQUIRED_EXPLICIT_ENV = TOPOLOGY_VARS | SECRET_OR_URL_VARS | {"MANIFEST"}
CTE_NAME_RE = re.compile(r"(?:\bWITH\b|,)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s+AS\s*\(", re.IGNORECASE)
CTE_REF_TEMPLATE = r"\b(?:FROM|JOIN)\s+{name}\b"
HEREDOC_START_RE = re.compile(r"<<'?([A-Z][A-Z0-9_]*)'?")
TEMP_OBJECT_RE = re.compile(r"\bCREATE\s+(?:TEMP|TEMPORARY)\b", re.IGNORECASE)
RUNNER_VERSION_RE = re.compile(r"p4d2_config_control_plane_targeted_proof_v(?P<version>[0-9]+)\.sh$")
EXPECTED_NEGATIVE_ASSIGNMENT_RE = re.compile(r"^[ \t]*(?:[A-Za-z_][A-Za-z0-9_]*_)?output=\"\$\(")



def _version_label_violations(path: Path, text: str) -> list[str]:
    violations: list[str] = []
    match = RUNNER_VERSION_RE.search(path.name)
    if not match:
        violations.append("runner filename must include p4d2_config_control_plane_targeted_proof_v<version>.sh")
        return violations
    version = match.group("version")
    expected_fragments = [
        f'EVIDENCE="/home/jeevashri/Downloads/p4d2_config_control_plane_targeted_proof_v{version}.txt"',
        f'PROOF_ROOT="$(mktemp -d /tmp/p4d2_config_control_plane_targeted_v{version}_XXXXXX)"',
        f'UTF8 V{version} PROOF',
    ]
    for fragment in expected_fragments:
        if fragment not in text:
            violations.append(f"runner version label mismatch; missing {fragment!r}")
    stale_versions = sorted(
        set(re.findall(r"p4d2_config_control_plane_targeted_(?:proof_)?v([0-9]+)", text)) - {version}
    )
    if stale_versions:
        violations.append(f"runner contains stale runner-owned version labels: {stale_versions!r}")
    return violations


def _expected_negative_capture_violations(text: str) -> list[str]:
    violations: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if "|| true" in line:
            violations.append(f"line {index}: decisive runner must not use || true")
        if not EXPECTED_NEGATIVE_ASSIGNMENT_RE.search(line):
            continue
        window = "\n".join(lines[max(0, index - 5): index + 8])
        if "set +e" in window:
            violations.append(
                f"line {index}: expected-negative command substitution uses set +e capture; "
                "use conditional assignment so global ERR trap cannot preempt classification"
            )
    required_fragments = [
        'if output="$(psql_super -d "$TEST_DB" -v ON_ERROR_STOP=1',
        'expected_failure_fragment',
        'grep -F "$expected_failure_fragment" <<<"$output"',
        'finish_blocked "direct finance.tax_codes INSERT unexpectedly succeeded for ${role_name}"',
        'expect_profile_insert_rejected_for_migration_owner',
        'grep -F "violates row-level security policy" <<<"$output"',
        'preserved_count="$(psql_super -d "$TEST_DB" -At -v ON_ERROR_STOP=1',
        'test "$preserved_count" = "0"',
        'if conflict_output="$(psql_super -d "$TEST_DB" -v ON_ERROR_STOP=1',
        'grep -F "P4D finance tax code configuration replay conflict" <<<"$conflict_output"',
    ]
    for fragment in required_fragments:
        if fragment not in text:
            violations.append(f"runner expected-negative capture missing fragment: {fragment}")
    return violations


def _shell_scope_violations(text: str) -> list[str]:
    violations: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        if "bash -c" not in line:
            continue
        prefix = line.split("bash -c", 1)[0]
        block_lines = [line]
        if "'" in line:
            for next_line in lines[index:]:
                block_lines.append(next_line)
                if next_line == "'":
                    break
        block = "\n".join(block_lines)
        referenced = {name for name in REQUIRED_EXPLICIT_ENV if f"${name}" in block or f"${{{name}}}" in block}
        missing = sorted(
            name
            for name in referenced
            if f'{name}="${{{name}}}"' not in prefix
            and f'{name}="$' + name + '"' not in prefix
            and f"export {name}" not in text
        )
        if missing:
            violations.append(f"line {index}: bash -c does not explicitly pass {missing!r}")
    return violations


def _sql_heredocs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    heredocs: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        match = HEREDOC_START_RE.search(lines[index])
        if not match:
            index += 1
            continue
        terminator = match.group(1)
        body_start = index + 1
        body: list[str] = []
        index += 1
        while index < len(lines) and lines[index] != terminator:
            body.append(lines[index])
            index += 1
        if terminator == "SQL":
            heredocs.append((body_start + 1, "\n".join(body)))
        index += 1
    return heredocs


def _split_sql_statements(sql: str) -> list[tuple[int, str]]:
    statements: list[tuple[int, str]] = []
    start_line = 1
    current: list[str] = []
    for offset, line in enumerate(sql.splitlines(), start=1):
        if not current and not line.strip():
            start_line = offset + 1
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statements.append((start_line, "\n".join(current)))
            current = []
            start_line = offset + 1
    if current:
        statements.append((start_line, "\n".join(current)))
    return statements


def _cte_scope_violations(text: str) -> list[str]:
    violations: list[str] = []
    for heredoc_line, sql in _sql_heredocs(text):
        prior_ctes: dict[str, int] = {}
        for relative_line, statement in _split_sql_statements(sql):
            absolute_line = heredoc_line + relative_line - 1
            defined = {match.group(1).lower() for match in CTE_NAME_RE.finditer(statement)}
            for name, defining_line in prior_ctes.items():
                if name in defined:
                    continue
                if re.search(CTE_REF_TEMPLATE.format(name=re.escape(name)), statement, re.IGNORECASE):
                    violations.append(
                        f"line {absolute_line}: SQL statement references CTE {name!r} "
                        f"defined only in earlier statement at line {defining_line}"
                    )
            for name in defined:
                prior_ctes[name] = absolute_line
    return violations


def _execute_closure_violations(text: str) -> list[str]:
    violations: list[str] = []
    if 'run_step "Function EXECUTE closure"' not in text:
        return violations
    if "SELECT bool_and(allowed = approved) AS exact_execute_closure FROM checks;" in text:
        violations.append("Function EXECUTE closure reuses checks CTE across statement boundary")
    if (
        "expected_closed_inventory(signature) AS" in text
        and "catalog_inventory(signature) AS" in text
        and "pg_catalog.pg_get_function_identity_arguments(p.oid)" in text
    ):
        violations.append(
            "Function EXECUTE closure compares type-only expected signatures "
            "against display-oriented pg_get_function_identity_arguments output"
        )
    if (
        "expected_closed_inventory(signature) AS" in text
        and "catalog_inventory(signature) AS" in text
        and "pg_catalog.oidvectortypes(p.proargtypes)" in text
    ):
        violations.append(
            "Function EXECUTE closure uses rendered oidvectortypes text as the "
            "decisive closed-inventory key"
        )
    if "'pg_catalog.integer'::pg_catalog.regtype::oid" in text:
        violations.append("Function EXECUTE closure schema-qualifies SQL alias pg_catalog.integer as a catalog type")
    forbidden_normalizers = ["replace(', ', ',')", "replace(pg_catalog.oidvectortypes", "regexp_replace"]
    for fragment in forbidden_normalizers:
        if fragment in text:
            violations.append(f"Function EXECUTE closure must not repair identity with textual normalization: {fragment}")
    required_fragments = [
        "expected_types(type_key, type_oid) AS",
        "('integer', 'pg_catalog.int4'::pg_catalog.regtype::oid)",
        "expected_functions(schema_name, function_name, arg_type_keys, approved) AS",
        "expected_closed_inventory AS",
        "arg_type_oids",
        "FROM unnest(f.arg_type_keys) WITH ORDINALITY",
        "JOIN expected_types t ON t.type_key = expected_arg.type_key",
        "p.proargtypes[argument.ordinality::integer - 1]",
        "missing_from_catalog AS",
        "unexpected_catalog_function AS",
        "matched_inventory AS",
        "matched.proc_oid",
        "pg_catalog.has_function_privilege(roles.role_name, matched.proc_oid, 'EXECUTE')",
        "expected_inventory_count_ok",
        "catalog_inventory_count_ok",
        "expected_minus_catalog_ok",
        "catalog_minus_expected_ok",
        "execute_matrix_count_ok",
        "approved_count_per_role_ok",
        "execute_closure_ok",
        "oid_native_inventory_and_execute_closure_assertion",
    ]
    for fragment in required_fragments:
        if fragment not in text:
            violations.append(f"Function EXECUTE closure missing OID-native structured fragment: {fragment}")
    return violations



def _lane_isolation_violations(text: str) -> list[str]:
    violations: list[str] = []
    required_names = [
        'TARGETED_TEST_DB="gymflow_p4d2_config_control_plane_targeted_test"',
        'P4D2_RUNTIME_TEST_DB="gymflow_p4d2_full_runtime_test"',
        'INHERITED_RUNTIME_TEST_DB="gymflow_p4d2_inherited_runtime_test"',
        'FINANCE_REGRESSION_TEST_DB="gymflow_p4d2_finance_regression_test"',
    ]
    for fragment in required_names:
        if fragment not in text:
            violations.append(f"runner lane-isolation missing database declaration: {fragment}")
    for lhs, rhs in [
        ('TARGETED_TEST_DB', 'P4D2_RUNTIME_TEST_DB'),
        ('TARGETED_TEST_DB', 'INHERITED_RUNTIME_TEST_DB'),
        ('TARGETED_TEST_DB', 'FINANCE_REGRESSION_TEST_DB'),
        ('P4D2_RUNTIME_TEST_DB', 'INHERITED_RUNTIME_TEST_DB'),
        ('P4D2_RUNTIME_TEST_DB', 'FINANCE_REGRESSION_TEST_DB'),
        ('INHERITED_RUNTIME_TEST_DB', 'FINANCE_REGRESSION_TEST_DB'),
    ]:
        if f'test "$${lhs}" != "$${rhs}"' in text:
            continue
        if f'test "${lhs}" != "${rhs}"' in text:
            continue
        if f'test "${{{lhs}}}" != "${{{rhs}}}"' not in text:
            violations.append(f"runner must assert distinct lane databases: {lhs} != {rhs}")
    for banner in [
        'database_lane "targeted/control-plane" "$TARGETED_TEST_DB"',
        'database_lane "p4d2-full-runtime" "$P4D2_RUNTIME_TEST_DB"',
        'database_lane "inherited-p4d1-runtime" "$INHERITED_RUNTIME_TEST_DB"',
        'database_lane "finance-regression" "$FINANCE_REGRESSION_TEST_DB"',
        'DATABASE LANE:',
        'host/port:',
    ]:
        if banner not in text:
            violations.append(f"runner database-lane banner missing fragment: {banner}")
    required_rebinds = [
        'bind_database_lane "$TARGETED_TEST_DB"',
        'bind_database_lane "$P4D2_RUNTIME_TEST_DB"',
        'bind_database_lane "$INHERITED_RUNTIME_TEST_DB"',
        'bind_database_lane "$FINANCE_REGRESSION_TEST_DB"',
    ]
    for fragment in required_rebinds:
        if fragment not in text:
            violations.append(f"runner URL rebind missing fragment: {fragment}")
    if text.find('run_step "Former blocker plus full P4D-2 runtime') > text.find('run_step "Inherited P4D-1 runtime'):
        # This ordering is allowed only with separate DBs. Require the separate inherited DB rebind before P4D-1.
        p4d1_index = text.find('run_step "Inherited P4D-1 runtime')
        if p4d1_index != -1 and text.rfind('bind_database_lane "$INHERITED_RUNTIME_TEST_DB"', 0, p4d1_index) == -1:
            violations.append("P4D-1 runtime lane must be rebound to inherited DB before execution")
    forbidden_runtime_grants = [
        'GRANT USAGE ON SCHEMA finance TO finance_test_runtime',
        'GRANT SELECT ON ALL TABLES',
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA finance TO finance_test_runtime',
        'GRANT ALL',
        'GRANT app_security_owner TO finance_test_runtime',
        'GRANT migration_owner TO finance_test_runtime',
    ]
    for fragment in forbidden_runtime_grants:
        if fragment in text:
            violations.append(f"runner must not grant broad Finance test runtime authority: {fragment}")

    protected_cleanup = [
        'DELETE FROM finance.branch_accounting_profiles',
        'DELETE FROM finance.membership_plan_tax_profiles',
        'DELETE FROM finance.tax_codes',
        'TRUNCATE TABLE finance.branch_accounting_profiles',
        'TRUNCATE TABLE finance.membership_plan_tax_profiles',
        'TRUNCATE TABLE finance.tax_codes',
    ]
    for fragment in protected_cleanup:
        if fragment in text:
            violations.append(f"runner must not clean protected finance configuration evidence: {fragment}")
    if re.search(r"^[ \t]*TRUNCATE\b[^\n;]*\bCASCADE\b", text, re.IGNORECASE | re.MULTILINE):
        violations.append("runner must not use TRUNCATE CASCADE")
    for fragment in re.findall(r"^[ \t]*TRUNCATE\b[^\n;]*", text, flags=re.IGNORECASE | re.MULTILINE):
        if "ALLOWLISTED_TEST_FIXTURE_TRUNCATE" not in fragment:
            violations.append(f"runner TRUNCATE must be explicitly allowlisted/reviewed: {fragment.strip()}")
    return violations

def _session_scope_violations(text: str) -> list[str]:
    violations: list[str] = []
    if TEMP_OBJECT_RE.search(text):
        violations.append("runner creates TEMP/TEMPORARY objects; static guard requires no temp-session residue")
    if re.search(r"\bSET\s+LOCAL\b", text, re.IGNORECASE):
        violations.append("runner uses SET LOCAL; static guard requires no transaction-local assertion residue")
    return violations


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify_p4d2_config_runner_scope.py RUNNER", file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8")
    violations: list[str] = []
    violations.extend(_version_label_violations(path, text))
    violations.extend(_expected_negative_capture_violations(text))
    violations.extend(_shell_scope_violations(text))
    violations.extend(_cte_scope_violations(text))
    violations.extend(_execute_closure_violations(text))
    violations.extend(_session_scope_violations(text))
    violations.extend(_lane_isolation_violations(text))
    if 'PGPORT_CHOSEN="$(choose_port)"' not in text:
        violations.append("runner must choose a non-default disposable port")
    if 'excluded = {5432,' not in text:
        violations.append("runner must explicitly exclude PostgreSQL default port 5432")
    if 'TEST_DB="gymflow_p4d_test"' in text:
        violations.append("runner must not use shared/default gymflow_p4d_test database")
    if re.search(r'psql[^\n]* -p 5432\b', text):
        violations.append("runner must not invoke psql against default port 5432")
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    print("P4D-2 config runner scope guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
