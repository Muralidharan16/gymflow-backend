
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"

MIG_0020 = VERSIONS / "0020_contacts_hardened.py"
MIG_0022 = VERSIONS / "0022_rbac_phase1_roles_extensions.py"
MIG_0028 = VERSIONS / "0028_rbac_p7_role_events.py"
MIG_FINANCE = (
    VERSIONS
    / "1a2b3c4d5e7f_finance_core_phase_5b_foundation.py"
)
MIG_BILLING = (
    VERSIONS
    / "f1a2b3c4d5e6_platform_billing_phase_1_foundation.py"
)

TARGETS = (
    MIG_0020,
    MIG_0022,
    MIG_0028,
    MIG_FINANCE,
    MIG_BILLING,
)

PGCRYPTO_ONLY = (
    "digest",
    "hmac",
    "crypt",
    "gen_salt",
    "gen_random_bytes",
    "pgp_sym_encrypt",
    "pgp_sym_decrypt",
    "pgp_sym_encrypt_bytea",
    "pgp_sym_decrypt_bytea",
    "pgp_pub_encrypt",
    "pgp_pub_decrypt",
    "pgp_pub_encrypt_bytea",
    "pgp_pub_decrypt_bytea",
    "pgp_key_id",
    "armor",
    "dearmor",
    "pgp_armor_headers",
    "encrypt",
    "decrypt",
    "encrypt_iv",
    "decrypt_iv",
)

POSTGRESQL16_CORE_CRYPTO = (
    "md5",
    "sha224",
    "sha256",
    "sha384",
    "sha512",
    "gen_random_uuid",
)

TEXT_SCAN_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}

SQL_HINT_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|"
    r"GRANT|REVOKE|FUNCTION|PROCEDURE|TRIGGER|POLICY|"
    r"EXTENSION|RETURNS|LANGUAGE|PLPGSQL|SQL)\b",
    re.IGNORECASE,
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _repository_text_entries() -> list[tuple[Path, str]]:
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-co",
            "--exclude-standard",
            "-z",
        ]
    )
    result = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = Path(
            item.decode("utf-8", "surrogateescape")
        )
        if any(
            part in TEXT_SCAN_EXCLUDED_PARTS
            for part in relative.parts
        ):
            continue
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.append((path, text))
    return result


def _python_sql_strings(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and SQL_HINT_RE.search(node.value)
    ]


def _fenced_sql_blocks(source: str) -> list[str]:
    return re.findall(
        r"```(?:sql|postgresql|plpgsql)\s*(.*?)```",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _repository_sql_fragments() -> list[tuple[Path, str]]:
    fragments = []
    for path, source in _repository_text_entries():
        suffix = path.suffix.lower()
        if suffix == ".py":
            values = _python_sql_strings(path, source)
        elif suffix == ".sql":
            values = [source]
        elif suffix in {".md", ".rst", ".txt"}:
            values = _fenced_sql_blocks(source)
        else:
            values = [
                source
                for _ in [0]
                if SQL_HINT_RE.search(source)
            ]
        fragments.extend((path, value) for value in values)
    return fragments


def _imports_sqlalchemy_as_sa(path: Path) -> bool:
    tree = ast.parse(_source(path), filename=str(path))
    return any(
        isinstance(node, ast.Import)
        and any(
            alias.name == "sqlalchemy"
            and alias.asname == "sa"
            for alias in node.names
        )
        for node in tree.body
    )

def _all_migration_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(VERSIONS.glob("*.py"))
    )


def _sql_strings(path: Path) -> list[str]:
    return _python_sql_strings(path, _source(path))



def test_pgcrypto_lifecycle_is_absent_repository_wide():
    pattern = re.compile(
        r"\b(?:CREATE|DROP)\s+EXTENSION\b[^;\n]*\bpgcrypto\b",
        re.IGNORECASE,
    )
    matches = [
        (path.relative_to(ROOT), fragment)
        for path, fragment in _repository_sql_fragments()
        if pattern.search(fragment)
    ]
    assert matches == []



def test_pgcrypto_only_sql_functions_are_absent():
    call_pattern = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(
            sorted(
                PGCRYPTO_ONLY,
                key=lambda value: (-len(value), value),
            )
        )
        + r")\s*\(",
        re.IGNORECASE,
    )

    fragments = _repository_sql_fragments()
    matches = [
        (path.relative_to(ROOT), fragment)
        for path, fragment in fragments
        if call_pattern.search(fragment)
    ]
    assert matches == []

    core_pattern = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(POSTGRESQL16_CORE_CRYPTO)
        + r")\s*\(",
        re.IGNORECASE,
    )
    core_symbols = {
        match.group(1).lower()
        for _, fragment in fragments
        for match in core_pattern.finditer(fragment)
    }
    assert "sha256" in core_symbols
    assert "gen_random_uuid" in core_symbols



def test_0020_defines_exact_shared_infrastructure_markers():
    source = _source(MIG_0020)
    assert (
        "app_private."
        "migration_0020_shared_infrastructure_state"
        in source
    )
    assert (
        "app_private.migration_0020_schema_acl_state"
        in source
    )
    for field in (
        "app_private_existed_before",
        "app_private_created_by_revision",
        "original_owner_oid",
        "original_owner_name",
        "expected_acl_operation_count",
        "state_finalized",
        "state_digest",
    ):
        assert field in source


def test_0020_preserves_preexisting_app_private_owner():
    source = _source(MIG_0020)
    assert "ALTER SCHEMA app_private OWNER" not in source
    assert "CREATE SCHEMA app_private " in source
    assert "AUTHORIZATION migration_owner" in source
    assert "app_private_created_by_revision" in source
    assert "Preexisting app_private OID or owner changed" in source


def test_0020_drops_created_app_private_restrict_only():
    source = _source(MIG_0020)
    assert 'sa.text("DROP SCHEMA app_private RESTRICT")' in source
    assert "DROP SCHEMA app_private CASCADE" not in source
    assert "app_private_created_by_revision" in source


def test_direct_acl_capture_uses_aclexplode_only():
    source = "\n".join(
        _source(path)
        for path in (MIG_0020, MIG_0022, MIG_0028)
    )
    assert "pg_catalog.aclexplode(" in source
    assert "pg_namespace" in source
    assert "nspacl" in source
    assert "acldefault(" not in source


def test_acl_markers_capture_grantor_and_grant_option():
    source = "\n".join(
        _source(path)
        for path in (MIG_0020, MIG_0022, MIG_0028)
    )
    for field in (
        "original_grantor_oid",
        "original_grantor_name",
        "original_is_grantable",
        "resulting_grantor_oid",
        "resulting_grantor_name",
        "resulting_is_grantable",
        "restoration_role_oid",
        "restoration_role_name",
    ):
        assert field in source


def test_acl_revoke_preflight_precedes_every_revoke():
    source = "\n".join(
        _source(path)
        for path in (MIG_0020, MIG_0022, MIG_0028)
    )
    assert "pg_catalog.pg_has_role(" in source
    assert "'SET'" in source
    assert "SET_LOCAL_ROLE_ORIGINAL_GRANTOR" in source
    assert "_rb1l7_restoration_context(bind, row)" in source
    assert "_rb1l7_revoke_sql(" in source


def test_public_pg_database_owner_is_not_assumed_restorable():
    source = _source(MIG_0020) + _source(MIG_0022)
    assert "pg_database_owner" not in source
    assert "grantor_name" in source
    assert "cannot SET ROLE" in source
    assert "has_grant_option" in source


def test_full_nspacl_restore_is_forbidden():
    source = "\n".join(_source(path) for path in TARGETS)
    forbidden = (
        "UPDATE pg_catalog.pg_namespace",
        "SET nspacl",
        "acldefault(",
    )
    for fragment in forbidden:
        assert fragment not in source
    assert "_rb1l7_restore_acl_rows" in source



def test_temporary_app_private_create_is_prestate_safe():
    source = _source(MIG_0020)
    assert "_rb1l7_prepare_temporary_app_private_create()" in source
    assert "_rb1l7_restore_temporary_app_private_create()" in source
    assert "_RB1L7_TEMP_CREATE_PRESTATE" in source
    assert "_RB1L7UpgradeOperations" in source
    assert '_rb1l7_upgrade_operations(globals()["op"])' in source
    assert "_rb1l7_execute_temporary_create_grant" in source
    assert "_rb1l7_execute_temporary_create_revoke" in source
    assert "Temporary app_private CREATE direct pre-state" in source
    assert (
        source.count(
            'op.execute("GRANT CREATE ON SCHEMA app_private '
            'TO app_rls_executor;")'
        )
        == 1
    )
    assert (
        source.count(
            'op.execute("REVOKE CREATE ON SCHEMA app_private '
            'FROM app_rls_executor;")'
        )
        == 1
    )


def test_revision_local_acl_markers_exist_for_0022_and_0028():
    source_0022 = _source(MIG_0022)
    source_0028 = _source(MIG_0028)

    assert (
        "app_private.migration_0022_schema_acl_state"
        in source_0022
    )
    assert (
        "app_private.migration_0028_schema_acl_state"
        in source_0028
    )
    for source in (source_0022, source_0028):
        assert "_rb1l7_load_acl_marker(bind)" in source
        assert "DROP TABLE " in source
        assert " RESTRICT" in source


def test_marker_validation_is_fail_closed():
    source = "\n".join(
        _source(path)
        for path in (MIG_0020, MIG_0022, MIG_0028)
    )
    for fragment in (
        "marker_version",
        "expected_operation_count",
        "state_finalized",
        "state_digest",
        "digest verification failed",
        "Marker collision",
    ):
        assert fragment in source


def test_0022_stale_app_private_origin_comment_is_removed():
    source = _source(MIG_0022)
    assert "created in 0002_enterprise_platform" not in source
    assert (
        "app_private is conditionally owned by "
        "0020_contacts_hardened"
        in source
    )
    assert "pgcrypto: required" not in source
    assert "pgcrypto (pinned)" not in source


def test_authorized_sa_text_migrations_import_sqlalchemy_as_sa():
    for path in TARGETS:
        source = _source(path)
        if "sa.text(" in source:
            assert _imports_sqlalchemy_as_sa(path), path


def test_marker_relations_and_identity_sequences_are_fail_closed():
    source = "\n".join(
        _source(path)
        for path in (MIG_0020, MIG_0022, MIG_0028)
    )
    assert "_rb1l7_assert_relation_isolated" in source
    assert "_rb1l7_assert_marker_isolated" in source
    assert "pg_catalog.pg_get_serial_sequence(" in source
    assert "relation_data.relacl" in source
    assert "acl_data.grantee = 0" in source
    assert "acl_data.grantee <> relation_data.relowner" in source
    assert "REVOKE ALL ON TABLE " not in source
    assert (
        source.count(
            '_rb1l7_assert_marker_isolated(\n'
            '        bind,\n'
            '        _RB1L7_ACL_MARKER,\n'
            '        identity_column="state_id",\n'
            '    )'
        )
        == 3
    )
    assert (
        '_rb1l7_assert_marker_isolated(\n'
        '        bind,\n'
        '        _RB1L7_HEADER_MARKER,\n'
        '    )'
        in _source(MIG_0020)
    )


def test_shared_infrastructure_requires_dual_migration_owner_principal():
    for path in (MIG_0020, MIG_0022, MIG_0028):
        source = _source(path)
        assert (
            'identity["session_user_name"] != "migration_owner"'
            in source
        )
        assert (
            'identity["current_user_name"] != "migration_owner"'
            in source
        )
        assert (
            'identity["session_user_oid"] '
            '!= identity["current_user_oid"]'
            in source
        )
        assert (
            'bind.execute(sa.text("RESET ROLE"))\n'
            '        _rb1l7_require_migration_owner(bind)'
            in source
        )

def test_shared_infrastructure_helpers_have_no_standalone_backslash_lines():
    for path in (MIG_0020, MIG_0022, MIG_0028):
        source = _source(path)
        helper_start = source.index(
            "# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START"
        )
        helper_end = source.index(
            "# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_END"
        )
        helper = source[helper_start:helper_end]
        standalone = [
            line_number
            for line_number, line in enumerate(
                helper.splitlines(),
                start=1,
            )
            if line.strip() == "\\"
        ]
        assert standalone == [], (path, standalone)

def test_shared_infrastructure_aclexplode_uses_direct_catalog_acl_columns():
    expected_counts = {
        MIG_0020: 3,
        MIG_0022: 2,
        MIG_0028: 2,
    }
    allowed_arguments = {
        "namespace_data.nspacl",
        "relation_data.relacl",
    }
    direct_call = re.compile(
        r"pg_catalog\.aclexplode\(\s*"
        r"([A-Za-z_][A-Za-z0-9_.]*)\s*\)",
        re.DOTALL,
    )
    all_arguments = []

    for path, expected_count in expected_counts.items():
        source = _source(path)
        helper_start = source.index(
            "# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_START"
        )
        helper_end = source.index(
            "# RB1L7_SHARED_INFRASTRUCTURE_HELPERS_END"
        )
        helper = source[helper_start:helper_end]

        assert "ARRAY[]::pg_catalog.aclitem[]" not in helper
        assert "acldefault(" not in helper
        assert re.search(
            r"pg_catalog\.aclexplode\(\s*COALESCE\s*\(",
            helper,
            flags=re.DOTALL,
        ) is None

        call_count = helper.count("pg_catalog.aclexplode(")
        arguments = direct_call.findall(helper)
        assert call_count == expected_count, (path, call_count)
        assert len(arguments) == expected_count, (path, arguments)
        assert set(arguments) <= allowed_arguments, (path, arguments)
        all_arguments.extend(arguments)

    assert len(all_arguments) == 7
    assert all_arguments.count("namespace_data.nspacl") == 4
    assert all_arguments.count("relation_data.relacl") == 3

def test_existing_managed_role_boundary_remains_separate():
    existing = (
        ROOT / "tests/test_migration_managed_role_boundary.py"
    )
    assert existing.is_file()
    source = _source(existing)
    assert "RB1L7_SHARED_INFRASTRUCTURE_HELPERS" not in source
    assert (
        Path(__file__).name
        == "test_migration_shared_infrastructure_boundary.py"
    )

# RB1L8D1F_BTREE_GIST_SHARED_LIFECYCLE_REGRESSION

import ast as _rb1l8d1f_ast
import hashlib as _rb1l8d1f_hashlib
import re as _rb1l8d1f_re
from pathlib import Path as _RB1L8D1FPath

_RB1L8D1F_ROOT = _RB1L8D1FPath(__file__).resolve().parents[1]
_RB1L8D1F_VERSIONS = _RB1L8D1F_ROOT / "alembic" / "versions"
_RB1L8D1F_0021 = _RB1L8D1F_VERSIONS / "0021_staff_roles.py"
_RB1L8D1F_0022 = (
    _RB1L8D1F_VERSIONS / "0022_rbac_phase1_roles_extensions.py"
)
_RB1L8D1F_EXPECTED_0021_SHA = (
    "c5bc24e259ea9938f680c8f5f41fe74c5a73ed1ffb7f9ab5b1a63946db6a702f"
)


def _rb1l8d1f_source(path: _RB1L8D1FPath) -> str:
    return path.read_text(encoding="utf-8")


def _rb1l8d1f_op_execute_sql(path: _RB1L8D1FPath) -> list[str]:
    source = _rb1l8d1f_source(path)
    tree = _rb1l8d1f_ast.parse(source, filename=str(path))
    result: list[str] = []
    for node in _rb1l8d1f_ast.walk(tree):
        if not isinstance(node, _rb1l8d1f_ast.Call):
            continue
        if not isinstance(node.func, _rb1l8d1f_ast.Attribute):
            continue
        if not isinstance(node.func.value, _rb1l8d1f_ast.Name):
            continue
        if node.func.value.id != "op" or node.func.attr != "execute":
            continue
        if not node.args:
            continue
        try:
            value = _rb1l8d1f_ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            result.append(" ".join(value.split()))
    return result


def test_0022_has_no_btree_gist_lifecycle_mutation_or_cascade_workaround():
    statements = _rb1l8d1f_op_execute_sql(_RB1L8D1F_0022)
    lifecycle = _rb1l8d1f_re.compile(
        r"\b(?:CREATE|DROP)\s+EXTENSION\b[^;]*\bbtree_gist\b",
        _rb1l8d1f_re.IGNORECASE,
    )
    matches = [statement for statement in statements if lifecycle.search(statement)]
    assert matches == []
    assert not any(
        _rb1l8d1f_re.search(
            r"\bDROP\s+EXTENSION\b[^;]*\bCASCADE\b",
            statement,
            _rb1l8d1f_re.IGNORECASE,
        )
        for statement in statements
    )


def test_0022_preserves_shared_dependency_owner_context_and_pgcrypto_contracts():
    source_0021 = _rb1l8d1f_source(_RB1L8D1F_0021)
    source_0022 = _rb1l8d1f_source(_RB1L8D1F_0022)

    observed_0021 = _rb1l8d1f_hashlib.sha256(
        _RB1L8D1F_0021.read_bytes()
    ).hexdigest()
    assert observed_0021 == _RB1L8D1F_EXPECTED_0021_SHA
    assert "exclude_overlapping_staff_assignments" in source_0021
    assert "EXCLUDE USING gist" in source_0021
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist;" in source_0021

    assert "RB1L8D1D2_APP_SECURE_OWNER_CONTEXT_HELPERS" in source_0022
    assert "_rb1l8d1d2_preflight_app_secure_owner_context" in source_0022
    assert "CREATE SCHEMA app_secure" in source_0022
    assert "AUTHORIZATION app_security_owner" in source_0022
    assert "_rb1l8d1d2_assert_app_secure_owner(bind)" in source_0022
    assert source_0022.count(
        '_rb1l7_run_as(\n        bind,\n        "app_security_owner",'
    ) == 6
    assert 'sa.text("RESET ROLE")' in source_0022
    assert source_0022.count(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure"
    ) == 2

    all_migration_sql: list[str] = []
    for path in sorted(_RB1L8D1F_VERSIONS.glob("*.py")):
        all_migration_sql.extend(_rb1l8d1f_op_execute_sql(path))
    pgcrypto_lifecycle = _rb1l8d1f_re.compile(
        r"\b(?:CREATE|DROP)\s+EXTENSION\b[^;]*\bpgcrypto\b",
        _rb1l8d1f_re.IGNORECASE,
    )
    assert [
        statement
        for statement in all_migration_sql
        if pgcrypto_lifecycle.search(statement)
    ] == []

    statements_0022 = _rb1l8d1f_op_execute_sql(_RB1L8D1F_0022)
    forbidden_role_mutation = _rb1l8d1f_re.compile(
        r"\b(?:CREATE|ALTER|DROP)\s+ROLE\b"
        r"|\bGRANT\s+app_security_owner\s+TO\b"
        r"|\bREVOKE\s+app_security_owner\s+FROM\b",
        _rb1l8d1f_re.IGNORECASE,
    )
    assert [
        statement
        for statement in statements_0022
        if forbidden_role_mutation.search(statement)
    ] == []

