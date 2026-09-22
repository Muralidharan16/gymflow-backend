from __future__ import annotations

from pathlib import Path


MIGRATION = Path(
    "alembic/versions/zz37d8e9f0a63_pay18_financial_observability.py"
)


def test_pay18_observability_acl_is_delta_tracked_and_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    required = (
        "app_private.pay18_financial_observability_acl_delta",
        "has_column_privilege",
        "ACL delta state already exists",
        "ACL delta state missing",
        "refusing to revoke predecessor authority",
        "INSERT INTO",
        "DROP TABLE app_private.pay18_financial_observability_acl_delta",
    )
    for token in required:
        assert token in source

    assert "REVOKE SELECT({rendered})" not in source
    assert "delta_rows.issubset(expected_grants)" in source
    assert "key not in expected_grants" in source


def test_pay18_acl_repair_does_not_broaden_runtime_role_authority() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "GRANT EXECUTE ON FUNCTION {_SNAPSHOT} TO {_MAINTENANCE}" in source
    assert "TO app_runtime" not in source
    assert "TO worker_runtime" not in source
    assert "SET row_security=on" in source
    assert "SECURITY DEFINER" in source
