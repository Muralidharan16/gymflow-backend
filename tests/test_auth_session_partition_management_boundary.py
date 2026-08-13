from __future__ import annotations

import ast
import re
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/3c4d5e6f7081_manage_auth_sessions_with_partman.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _literal_text() -> str:
    tree = ast.parse(_source())
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_revision_is_new_head_after_organization_idempotency() -> None:
    source = _source()

    assert 'revision: str = "3c4d5e6f7081"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "2b3c4d5e6f70"' in source


def test_extension_remains_infrastructure_owned() -> None:
    literal_text = _literal_text().upper()

    assert "CREATE EXTENSION" not in literal_text
    assert "DROP EXTENSION" not in literal_text
    assert "ALTER EXTENSION" not in literal_text
    assert "ALTER EXTENSION" not in _source().upper()
    assert 'if row["owner_name"] == _MIGRATION_OWNER' in _source()


def test_migration_refuses_privilege_escalation_and_broad_grants() -> None:
    source = _source()
    literal_text = _literal_text()

    for token in (
        "is_superuser",
        "bypasses_rls",
        "can_create_database",
        "can_create_role",
        "can_replicate",
    ):
        assert token in source

    # Catalog aliases such as ``is_superuser`` and ``bypasses_rls`` are
    # evidence that the migration rejects elevated execution identities. They
    # must not be confused with DDL that *grants* those attributes.
    forbidden_role_escalation = (
        r"\bCREATE\s+(?:ROLE|USER)\b[^;]*\bSUPERUSER\b",
        r"\bALTER\s+(?:ROLE|USER)\b[^;]*\bSUPERUSER\b",
        r"\bCREATE\s+(?:ROLE|USER)\b[^;]*\bBYPASSRLS\b",
        r"\bALTER\s+(?:ROLE|USER)\b[^;]*\bBYPASSRLS\b",
        r"\bCREATE\s+(?:ROLE|USER)\b[^;]*\bCREATEDB\b",
        r"\bALTER\s+(?:ROLE|USER)\b[^;]*\bCREATEDB\b",
        r"\bCREATE\s+(?:ROLE|USER)\b[^;]*\bCREATEROLE\b",
        r"\bALTER\s+(?:ROLE|USER)\b[^;]*\bCREATEROLE\b",
        r"\bGRANT\s+ALL\b",
        r"\bSET(?:\s+LOCAL)?\s+ROLE\b",
        r"\bSET\s+SESSION\s+AUTHORIZATION\b",
    )
    for pattern in forbidden_role_escalation:
        assert not re.search(pattern, literal_text, re.IGNORECASE | re.DOTALL)


def test_upgrade_adopts_legacy_partition_without_dropping_it() -> None:
    source = _source()

    assert '_LEGACY_CHILD = "auth_sessions_y2026_m05"' in source
    assert '_CANONICAL_LEGACY_CHILD = "auth_sessions_p20260501"' in source
    assert "ALTER TABLE " in source
    assert "RENAME TO" in source
    assert "DROP TABLE public.auth_sessions_y2026_m05" not in source


def test_partman_contract_is_monthly_forward_maintained_and_fail_visible() -> None:
    source = _source()

    assert "p_control := 'created_at'" in source
    assert "p_interval := '1 month'" in source
    assert "p_default_table := false" in source
    assert "p_automatic_maintenance := 'on'" in source
    assert "infinite_time_partitions = true" in source
    assert "retention = NULL" in source
    assert "retention_keep_table = true" in source
    assert "p_jobmon := false" in source
    assert "_PREMAKE = 12" in source
    assert '"default_table": False' in source


def test_upgrade_proves_current_month_partition_and_no_default() -> None:
    source = _source()

    assert "partman.show_partition_name" in source
    assert "CURRENT_TIMESTAMP::text" in source
    assert 'if current is None or not current["table_exists"]' in source
    assert '_DEFAULT_RELATION = "public.auth_sessions_default"' in source
    assert "pg_catalog.to_regclass(:relation_name)::text" in source
    assert "unexpectedly created a DEFAULT partition" in source


def test_downgrade_fails_closed_before_removing_session_partitions() -> None:
    source = _source()
    safety = source.split("def _require_downgrade_safe", 1)[1].split(
        "\ndef upgrade() -> None:", 1
    )[0]

    # Every non-historical generated child is counted before it is admitted to
    # the removable set. A non-zero row count raises before downgrade reaches
    # partman config deletion or DROP TABLE.
    assert "SELECT count(*) FROM" in safety
    assert "if row_count != 0:" in safety
    assert "raise RuntimeError(" in safety
    assert "contains {row_count} row(s)" in safety
    assert "be dropped by rollback." in safety
    assert "removable.append(name)" in safety
    assert safety.index("if row_count != 0:") < safety.index("removable.append(name)")

    downgrade = source.split("def downgrade() -> None:", 1)[1]
    assert "_require_downgrade_safe(bind)" in downgrade
    assert "DELETE FROM partman.part_config" in downgrade
    assert "DROP TABLE" in downgrade
    assert downgrade.index("_require_downgrade_safe(bind)") < downgrade.index(
        "DELETE FROM partman.part_config"
    )
    assert downgrade.index("DELETE FROM partman.part_config") < downgrade.index(
        "DROP TABLE"
    )


def test_downgrade_never_drops_parent_or_extension() -> None:
    source = _source()
    downgrade = source.split("def downgrade() -> None:", 1)[1].upper()

    assert "DROP EXTENSION" not in downgrade
    assert "DROP TABLE PUBLIC.AUTH_SESSIONS" not in downgrade
    assert "DROP TABLE IF EXISTS PUBLIC.AUTH_SESSIONS" not in downgrade
