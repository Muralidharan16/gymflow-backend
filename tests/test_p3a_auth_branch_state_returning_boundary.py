from __future__ import annotations

from pathlib import Path


MIGRATION = Path(
    "alembic/versions/c77d8e9f0a21_p3a_auth_branch_state_returning_boundary.py"
)
MODEL = Path("app/models/org_branch.py")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_c77_extends_c67_as_single_p3a_head() -> None:
    source = _source(MIGRATION)
    assert 'revision = "c77d8e9f0a21"' in source
    assert 'down_revision = "c67d8e9f0a20"' in source


def test_auth_state_relation_remains_insert_only() -> None:
    source = _source(MIGRATION)
    assert '_EXPECTED_TABLE_ACL = {"INSERT"}' in source
    assert "broad org_branch_state SELECT" in source
    assert 'forbidden_privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")' in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source


def test_only_sqlalchemy_returning_columns_are_granted() -> None:
    source = _source(MIGRATION)
    model_source = _source(MODEL)

    assert '_RETURNING_COLUMNS = ("status_changed_at", "updated_at")' in source
    assert (
        "GRANT SELECT (status_changed_at, updated_at) "
        '"\n            "ON TABLE public.org_branch_state TO auth_runtime"'
    ) in source

    assert "status_changed_at: Mapped[datetime]" in model_source
    assert 'server_default=text("clock_timestamp()")' in model_source
    assert "updated_at: Mapped[datetime]" in model_source
    assert 'server_default=text("now()")' in model_source


def test_c77_is_reversible_and_proves_exact_column_acl() -> None:
    source = _source(MIGRATION)
    assert "acl_data.is_grantable" in source
    assert "grantor_role.rolname" in source
    assert '(column_name, "SELECT", False, _MIGRATION_OWNER)' in source
    assert "REVOKE SELECT (status_changed_at, updated_at)" in source
    assert source.count("_require_predecessor(bind)") >= 2
    assert source.count("_require_forward(bind)") >= 2


def test_state_relation_stays_force_rls() -> None:
    source = _source(MIGRATION)
    assert "relrowsecurity" in source
    assert "relforcerowsecurity" in source
    assert "org_branch_state must retain ENABLE + FORCE RLS" in source
