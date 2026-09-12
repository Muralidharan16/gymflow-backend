from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "6f708192a3b4_address_runtime_privilege_boundary.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _downgrade(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_6f_internal_sequences_are_protected_owner_capabilities() -> None:
    source = _source()
    upgrade = _upgrade(source)
    downgrade = _downgrade(source)

    assert '"branch_address_history_id_seq": ("branch_address_history", "id")' in source
    assert '"address_change_outbox_id_seq": ("address_change_outbox", "id")' in source
    assert "_require_internal_sequence_contract(bind)" in upgrade
    assert "_require_internal_sequence_contract(bind)" in downgrade

    assert '"GRANT USAGE ON SEQUENCE "' in upgrade
    assert '"REVOKE USAGE ON SEQUENCE "' in downgrade
    for sequence_name in (
        "public.branch_address_history_id_seq",
        "public.address_change_outbox_id_seq",
    ):
        assert sequence_name in upgrade
        assert sequence_name in downgrade
    assert '"public.address_change_outbox_id_seq TO app_security_owner"' in upgrade
    assert '"public.address_change_outbox_id_seq FROM app_security_owner"' in downgrade
    assert "TO app_runtime" not in upgrade.split('"GRANT USAGE ON SEQUENCE "', 1)[1]


def test_6f_forward_and_predecessor_validate_sequence_privileges() -> None:
    source = _source()

    assert "def _effective_sequence_privileges" in source
    assert "has_sequence_privilege" in source
    assert "app_security_owner sequence ACL drifted" in source
    assert "app_runtime must not have sequence privileges" in source
    assert "predecessor unexpectedly grants sequence privileges" in source
    assert "pg_get_serial_sequence" in source
    assert "unexpected owner for internal sequence" in source
