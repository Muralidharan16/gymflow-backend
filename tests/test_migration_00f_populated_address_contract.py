from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "00f277c748ea_add_hyperscale_branch_name_and_address_.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _upgrade_source(source: str) -> str:
    return source[source.index("def upgrade() -> None:") : source.index("def downgrade() -> None:")]


def _preflight_downgrade_source(source: str) -> str:
    return source[
        source.index("def _preflight_downgrade() -> None:") :
        source.index("def _restore_address_predecessor_security() -> None:")
    ]


def _downgrade_source(source: str) -> str:
    return source[source.index("def downgrade() -> None:") :]


def test_00f_never_deletes_legacy_addresses_to_satisfy_branch_id() -> None:
    source = _source()

    assert "TRUNCATE TABLE organization_addresses" not in source
    assert "DELETE FROM public.organization_addresses" not in source
    assert "DELETE FROM organization_addresses" not in source


def test_00f_forward_keeps_rls_enabled_and_uses_tenant_scoped_backfill() -> None:
    source = _source()
    upgrade = _upgrade_source(source)
    downgrade = _downgrade_source(source)

    assert "set_config(" in source
    assert "'app.current_org_id'" in source
    assert "ENABLE ROW LEVEL SECURITY" in upgrade
    assert "FORCE ROW LEVEL SECURITY" in upgrade
    assert "DISABLE ROW LEVEL SECURITY" not in upgrade
    assert "NO FORCE ROW LEVEL SECURITY" not in upgrade
    assert "SET row_security = off" not in source
    assert "SET row_security TO off" not in source

    # 0009 had no address RLS. A correct inverse must explicitly restore that
    # predecessor posture rather than leaving 00f security flags behind.
    assert "NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "DISABLE ROW LEVEL SECURITY" in downgrade


def test_00f_is_expand_only_for_predecessor_address_storage() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    predecessor_columns = (
        "is_verified",
        "verified_at",
        "verification_source",
        "coordinates",
        "coordinates_source",
        "is_primary",
        "geocoding_failed",
        "effective_from",
        "effective_until",
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
    )

    for column in predecessor_columns:
        assert f"DROP COLUMN {column}" not in upgrade
        assert f"drop_column(\"organization_addresses\", \"{column}\"" not in upgrade


def test_00f_installs_composite_fk_before_populated_backfill() -> None:
    source = _source()
    upgrade = _upgrade_source(source)

    # Keep this contract about migration semantics and operation ordering, not
    # about black/formatter line wrapping. The FK anchor is intentionally the
    # named composite constraint so a single-column branch FK cannot satisfy it.
    branch_column = upgrade.index(
        'op.add_column("organization_addresses", sa.Column("branch_id", sa.UUID(), nullable=True))'
    )
    composite_fk = upgrade.index("op.create_foreign_key(_BRANCH_ORG_FK,")
    backfill = upgrade.index("_backfill_legacy_addresses()")
    not_null = upgrade.index(
        'op.alter_column("organization_addresses", "branch_id"'
    )

    assert branch_column < composite_fk < backfill < not_null
    assert '["branch_id", "org_id"], ["id", "org_id"]' in upgrade
    assert 'ondelete="RESTRICT"' in upgrade
    assert "ForeignKey(\"org_branches.id\"" not in source


def test_00f_composite_fk_is_explicit_validated_and_reversible() -> None:
    source = _source()

    assert '_BRANCH_ORG_FK = "fk_org_addresses_branch_org"' in source
    assert "constraint_data.conname = 'fk_org_addresses_branch_org'" in source
    assert "constraint_data.convalidated" in source
    assert (
        "FOREIGN KEY (branch_id, org_id) REFERENCES org_branches(id, org_id) ON DELETE RESTRICT"
        in source
    )
    assert (
        'op.drop_constraint(_BRANCH_ORG_FK, "organization_addresses", '
        'type_="foreignkey", schema="public")'
        in source
    )


def test_00f_downgrade_fails_closed_on_unrepresentable_new_state() -> None:
    source = _source()

    assert "_preflight_downgrade()" in source
    assert "would lose address state that 0009 cannot represent" in source
    assert "would discard populated 00f-only relation" in source
    assert "would lose diverged geolocation state" in source


def test_00f_downgrade_preflights_predecessor_owned_branch_references() -> None:
    source = _source()
    preflight = _preflight_downgrade_source(source)

    assert "FROM public.branch_audit_log AS audit_data" in preflight
    assert "JOIN public.org_branches AS branch_data" in preflight
    assert "migration_00f_legacy_backfill" in preflight
    assert "predecessor-owned branch_audit_log references synthesized branch" in preflight

    # 0006 owns this audit history. 00f may refuse rollback when it references
    # a migration-owned synthetic branch, but it must never erase predecessor
    # evidence just to make its own downgrade succeed.
    assert "DELETE FROM public.branch_audit_log" not in source
    assert "TRUNCATE TABLE branch_audit_log" not in source
