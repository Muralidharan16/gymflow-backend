from __future__ import annotations

from pathlib import Path


SOURCE = Path("app/models/address.py")


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_address_models_never_probe_database_during_import() -> None:
    source = _source()

    for forbidden in (
        "check_postgis_available",
        "DISABLE_POSTGIS",
        "create_engine(",
        ".connect()",
        "pg_extension",
        'replace("+asyncpg", "")',
    ):
        assert forbidden not in source


def test_spatial_metadata_is_deterministic_postgis_geography() -> None:
    source = _source()

    assert (
        'coordinate_type = geoalchemy2.Geography(geometry_type="POINT", srid=4326)'
        in source
    )
    assert "coordinate_type = sa.String" not in source
    assert source.count("mapped_column(coordinate_type") >= 2


def test_typed_audit_principal_registry_is_mapped_without_pii() -> None:
    source = _source()

    start = source.index("class AuditPrincipal(Base):")
    end = source.index("class OrganizationAddress", start)
    model = source[start:end]

    for column in ("principal_id", "org_id", "principal_type", "registered_at"):
        assert column in model
    assert "email" not in model
    assert "phone" not in model
    assert "ck_audit_principals_type" in model


def test_address_actor_foreign_keys_are_tenant_and_namespace_scoped() -> None:
    source = _source()

    assert 'ForeignKey("gym_owners.id"' not in source
    for constraint_name in (
        "fk_organization_addresses_deleted_audit_principal",
        "fk_branch_address_history_audit_principal",
        "fk_branch_address_audit_audit_principal",
    ):
        assert constraint_name in source

    assert source.count('"audit_principals.principal_id"') == 3
    assert source.count('"audit_principals.org_id"') == 3
    assert source.count('"audit_principals.principal_type"') == 3
    assert "deleted_by_type" in source
    assert source.count("changed_by_type") >= 2
