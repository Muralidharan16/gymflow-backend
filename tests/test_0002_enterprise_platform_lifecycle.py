from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0002_enterprise_platform.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade_source(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_0002_upgrade_refuses_silent_object_adoption() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    assert "_preflight_upgrade()" in upgrade
    assert "already exists; refusing adoption" in source
    assert "to_regclass('public.' || relation_name)" in source
    assert "IF NOT EXISTS" not in upgrade


def test_0002_downgrade_refuses_populated_revision_owned_state() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "_preflight_downgrade()" in downgrade
    assert "downgrade would discard populated revision-owned relation" in source
    assert "SELECT EXISTS (SELECT 1 FROM public.%I LIMIT 1)" in source


def test_0002_downgrade_never_cascades_or_masks_missing_objects() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "CASCADE" not in downgrade
    assert "IF EXISTS" not in downgrade
    assert downgrade.count(" RESTRICT") == 6
