from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M66 = ROOT / "alembic/versions/66a95af89112_init_partman_for_branch_lifecycle_events.py"


def _source() -> str:
    return M66.read_text(encoding="utf-8")


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(_source(), filename=str(M66))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _function_source(name: str) -> str:
    source = _source()
    node = _function(name)
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def test_66a_treats_pg_partman_as_external_infrastructure() -> None:
    source = _source()
    assert "CREATE EXTENSION" not in source.upper()
    assert "DROP EXTENSION" not in source.upper()
    assert "CREATE SCHEMA IF NOT EXISTS partman" not in source
    for token in (
        "pg_catalog.pg_extension",
        "extension.extversion",
        '_PARTMAN_VERSION = "5.0.1"',
        "pg_catalog.to_regprocedure",
        "partman.create_parent(",
        "has_function_privilege",
        "must not own the extension",
    ):
        assert token in source


def test_66a_never_drops_predecessor_data_partitions_on_upgrade() -> None:
    upgrade = _function_source("upgrade")
    source = _source()
    assert "DROP TABLE IF EXISTS public.branch_lifecycle_events_2026_q2" not in source
    assert "DROP TABLE IF EXISTS public.branch_lifecycle_events_2026_q3" not in source
    assert "undo_partition_proc" not in source
    assert "_rename_partitions(bind, _LEGACY_TO_PARTMAN)" in upgrade
    assert '"branch_lifecycle_events_2026_q2"' in source
    assert '"branch_lifecycle_events_p20260401"' in source
    assert '"branch_lifecycle_events_2026_q3"' in source
    assert '"branch_lifecycle_events_p20260701"' in source


def test_66a_locks_and_validates_exact_predecessor_before_rename() -> None:
    helper = _function_source("_lock_and_require_predecessor_partition_set")
    for token in (
        "LOCK TABLE",
        "IN ACCESS EXCLUSIVE MODE",
        '"relation_kind": "p"',
        '"owner_name": _MIGRATION_OWNER',
        '"partition_key": "RANGE (emitted_at)"',
        "2026-04-01",
        "2026-07-01",
        "2026-10-01",
        "relations already exist",
    ):
        assert token in helper or token in _source()


def test_66a_registers_existing_set_with_aligned_partman_contract() -> None:
    helper = _function_source("_configure_partman")
    for token in (
        "p_control := 'emitted_at'",
        "p_interval := '3 months'",
        "p_type := 'range'",
        "p_premake := 4",
        "p_start_partition := '2026-04-01 00:00:00+00'",
        "p_default_table := false",
        "p_template_table := 'false'",
        "p_jobmon := false",
        "retention = '2 years'",
        "retention_keep_table = false",
        "partition_interval::interval = interval '3 months'",
        "retention::interval = interval '2 years'",
    ):
        assert token in helper
    assert '"partition_interval": "3 mons"' not in _source()


def test_66a_downgrade_fails_closed_before_dropping_generated_partitions() -> None:
    helper = _function_source("_remove_partman_management")
    assert "_require_extra_partitions_empty(bind, extras)" in helper
    assert "DELETE FROM partman.part_config" in helper
    assert "DROP TABLE " in helper
    assert "RESTRICT" in helper
    assert helper.index("_require_extra_partitions_empty(bind, extras)") < helper.index(
        "DELETE FROM partman.part_config"
    ) < helper.index("DROP TABLE ")

    emptiness = _function_source("_require_extra_partitions_empty")
    assert "SELECT EXISTS (SELECT 1 FROM ONLY " in emptiness
    assert "Refusing destructive 66a downgrade" in emptiness


def test_66a_downgrade_restores_legacy_partition_names_without_unpartitioning() -> None:
    source = _source()
    helper = _function_source("_remove_partman_management")
    assert "undo_partition_proc" not in source
    assert "reverse_mapping" in helper
    assert "_rename_partitions(bind, reverse_mapping)" in helper
    assert "config_count" in helper


def test_66a_contains_no_superuser_or_role_escalation_workaround() -> None:
    source = _source()
    forbidden = (
        r"\bALTER\s+ROLE\b",
        r"\bCREATE\s+ROLE\b",
        r"\bGRANT\s+[^;]*\bTO\s+migration_owner\b",
        r"\bSET(?:\s+LOCAL)?\s+ROLE\b",
        r"\bSUPERUSER\b[^\n]*;",
        r"\bBYPASSRLS\b[^\n]*;",
    )
    for pattern in forbidden:
        assert re.search(pattern, source, re.IGNORECASE) is None, pattern


def test_66a_upgrade_and_downgrade_are_online_catalog_driven() -> None:
    upgrade = _function_source("upgrade")
    downgrade = _function_source("downgrade")
    assert "op.get_bind()" in upgrade
    assert "op.get_bind()" in downgrade
    assert "_require_migration_identity(bind)" in upgrade
    assert "_require_registered_partition_set(bind)" in downgrade
