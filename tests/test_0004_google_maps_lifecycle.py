from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0004_google_maps_integration.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_0004_downgrade_blocks_non_default_durable_maps_state() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "_preflight_downgrade()" in downgrade
    assert "would discard populated durable Google Maps state" in source
    for column in (
        "google_place_id",
        "latitude",
        "longitude",
        "maps_embed_allowed",
        "maps_verification_status",
        "maps_last_verified_at",
        "maps_verification_error",
        "maps_verification_source",
        "maps_updated_at",
        "maps_next_retry_at",
        "maps_retry_count",
    ):
        assert f"'{column}'" in source


def test_0004_cache_is_explicitly_disposable_but_never_cascaded() -> None:
    source = _source()
    downgrade = _downgrade_source(source)

    assert "rebuildable" in source
    assert "DROP TABLE public.google_places_cache RESTRICT" in downgrade
    assert "CASCADE" not in downgrade


def test_0004_downgrade_requires_exact_owned_objects_before_teardown() -> None:
    source = _source()

    assert "0004 downgrade drift: public.google_places_cache is missing" in source
    assert "0004 downgrade drift: organization_addresses.% is missing" in source
    assert "0004 downgrade drift: constraint % is missing" in source
    assert "0004 downgrade drift: index public.% is missing" in source
