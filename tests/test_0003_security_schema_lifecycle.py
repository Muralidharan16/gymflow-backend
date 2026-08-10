from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0003_security_schemas.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade_source(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_0003_upgrade_refuses_silent_security_object_adoption() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    assert "_preflight_upgrade()" in upgrade
    assert "already exists; refusing adoption" in source
    assert "encryption_key_registry_key_version_seq" in source
    assert "address_audit_ledger_id_seq" in source
    assert "IF NOT EXISTS" not in upgrade


def test_0003_downgrade_refuses_populated_security_or_audit_state() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "_preflight_downgrade()" in downgrade
    assert "downgrade would discard populated security/audit relation" in source
    for relation in (
        "encryption_key_registry",
        "organization_address_payloads_secure",
        "address_audit_ledger",
        "audit_chain_heads",
    ):
        assert f"'{relation}'" in source


def test_0003_downgrade_is_dependency_ordered_and_never_cascades() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "CASCADE" not in downgrade
    assert "IF EXISTS" not in downgrade
    assert downgrade.count(" RESTRICT") == 4
    assert downgrade.index("DROP TABLE public.audit_chain_heads RESTRICT") < downgrade.index(
        "DROP TABLE public.address_audit_ledger RESTRICT"
    )
    assert downgrade.index(
        "DROP TABLE public.organization_address_payloads_secure RESTRICT"
    ) < downgrade.index("DROP TABLE public.encryption_key_registry RESTRICT")
