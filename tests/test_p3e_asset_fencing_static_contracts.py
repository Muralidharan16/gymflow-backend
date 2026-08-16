from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_asset_api_persists_authority_before_opaque_queue_publish() -> None:
    source = _source("app/routers/assets.py")
    assert "app_secure.enqueue_organization_asset_job" in source
    assert "await db.commit()" in source
    assert "process_organization_asset.delay(str(job_id))" in source
    assert "process_org_logo.delay" not in source
    assert "process_org_cover.delay" not in source
    assert 'f"quarantine/{current_staff.org_id}/{upload_id}"' in source
    assert 'principal_type", None) != "owner"' in source
    assert 'settings.ENVIRONMENT != "development"' in source
    assert "delete_current_organization_asset" in source
    assert "delete_old_s3_assets" not in source


def test_asset_worker_uses_database_claim_and_deterministic_keys_only() -> None:
    source = _source("app/tasks/base_image.py")
    assert "app_secure.claim_organization_asset_job" in source
    assert "app_secure.finalize_organization_asset_job" in source
    assert "app_secure.fail_organization_asset_job" in source
    assert "WorkerSyncSessionLocal" in source
    assert "db.query(Organization)" not in source
    assert "OrganizationAssetAudit" not in source
    assert "asset_uuid = uuid.uuid4" not in source
    assert 'f"originals/{org_id}/{upload_id}_original"' in source
    assert 'f"quarantine/{org_id}/{upload_id}"' in source
    assert "working = img.copy()" in source
    assert "scale = max(target_width / img.width, target_height / img.height)" in source


def test_arbitrary_key_and_legacy_authority_queue_contracts_fail_closed() -> None:
    logos = _source("app/tasks/logos.py")
    covers = _source("app/tasks/covers.py")
    assert 'name="app.tasks.logos.process_organization_asset"' in logos
    assert "def process_organization_asset(job_id: str)" in logos
    assert "claim_organization_asset_cleanup" in logos
    assert "complete_organization_asset_cleanup" in logos
    assert "def delete_old_s3_assets(*_args, **_kwargs)" in logos
    assert "Arbitrary queued S3-key deletion is disabled" in logos
    assert "def cleanup_orphaned_logos(*_args, **_kwargs)" in logos
    assert "Legacy global orphan-logo cleanup is disabled" in logos
    assert "def process_org_logo(*_args, **_kwargs)" in logos
    assert "def process_org_cover(*_args, **_kwargs)" in covers


def test_asset_maintenance_only_dispatches_opaque_ids() -> None:
    maintenance = _source("app/tasks/platform_maintenance.py")
    celery = _source("app/core/celery_app.py")
    assert "dispatchable_organization_asset_jobs(100)" in maintenance
    assert "dispatchable_organization_asset_cleanup(200)" in maintenance
    assert 'args=[str(row["job_id"])]' in maintenance
    assert 'args=[str(row["cleanup_id"])]' in maintenance
    assert "s3_key" not in maintenance
    assert "dispatch_organization_asset_jobs" in celery
    assert "dispatch_organization_asset_cleanup" in celery
    assert '"app.tasks.logos"' in celery
    assert '"app.tasks.covers"' in celery
    assert '"app.tasks.platform_maintenance"' in celery
    assert '"organization-asset-redispatch"' in celery
    assert '"organization-asset-cleanup-dispatch"' in celery


def test_asset_migrations_keep_reduced_role_and_identity_domain_contracts() -> None:
    n07 = _source("alembic/versions/n07d8e9f0a2e_p3e_fenced_organization_asset_jobs.py")
    o07 = _source("alembic/versions/o07d8e9f0a2f_p3e_asset_delete_capability.py")
    p07 = _source("alembic/versions/p07d8e9f0a30_p3e_asset_cleanup_jobs.py")
    q07 = _source("alembic/versions/q07d8e9f0a31_p3e_asset_claim_ambiguity.py")
    r07 = _source("alembic/versions/r07d8e9f0a32_p3e_modern_owner_asset_provenance.py")
    s07 = _source("alembic/versions/s07d8e9f0a33_p3e_asset_live_owner_authority.py")
    t07 = _source("alembic/versions/t07d8e9f0a34_p3e_asset_status_enum_recovery.py")
    combined = "\n".join((n07, o07, p07, q07, r07, s07, t07)).lower()
    normalized_p07 = " ".join(p07.lower().split())

    assert "bypassrls" in combined
    assert " bypassrls;" not in combined
    assert "alter role" not in combined
    assert "row_security = on" in combined
    assert "security definer" in combined
    assert "for update skip locked" in combined
    assert "grant execute on function" in combined
    assert "grant select on table public.organization_asset_jobs to worker_runtime" not in combined
    assert "grant update on table public.organization_asset_jobs to worker_runtime" not in combined
    assert "pg_catalog.coalesce" not in combined
    assert "pg_catalog.nullif" not in combined
    assert "pg_catalog.greatest" not in combined

    assert "current_principal_type" in n07
    assert "requested_by_owner_id" in n07
    assert "lease_token" in n07
    assert "superseded" in n07
    assert "delete_current_organization_asset" in o07
    assert "capture_organization_asset_key_cleanup" in p07
    assert "capture_organization_asset_job_cleanup" in p07
    assert "dispatchable_organization_asset_cleanup" in p07
    assert "has_schema_privilege" in p07
    assert "grant usage on schema app_secure to migration_owner" in normalized_p07
    assert "revoke usage on schema app_secure from migration_owner" in normalized_p07
    assert "revoke execute on function" in normalized_p07
    assert "from migration_owner" in normalized_p07
    assert "attempt_count = job.attempt_count + 1" in q07
    assert "_claim_contract" in q07
    for token in (
        "worker_execute",
        "api_execute",
        "auth_execute",
        "maintenance_execute",
        "public_execute",
    ):
        assert token in q07

    assert '"organizations_logo_updated_by_fkey"' in r07
    assert '("organizations", "logo_updated_by", "gym_owners", "id")' in r07
    assert '"organizations_cover_updated_by_fkey"' in r07
    assert '("organizations", "cover_updated_by", "gym_owners", "id")' in r07
    assert '"organization_asset_audit_changed_by_fkey"' in r07
    assert '("organization_asset_audit", "changed_by", "gym_owners", "id")' in r07
    assert "logo_updated_by_owner_id" in r07
    assert "cover_updated_by_owner_id" in r07
    assert "changed_by_owner_id" in r07
    assert '"owners", "id"' in r07
    assert "changed_by IS NULL OR changed_by_owner_id IS NULL" in r07
    assert "logo_updated_by = NULL" in r07
    assert "cover_updated_by = NULL" in r07

    assert 'down_revision = "r07d8e9f0a32"' in s07
    assert "email_verified IS TRUE" in s07
    assert "has_column_privilege" in s07
    assert "_require_inherited_owner_read" in s07
    assert "GRANT SELECT (email_verified)" not in s07
    assert "REVOKE SELECT (email_verified)" not in s07
    assert "SET LOCAL ROLE app_security_owner" in s07

    assert 'down_revision = "s07d8e9f0a33"' in t07
    assert "'ready'::public.asset_status_enum" in t07
    assert "SET LOCAL ROLE app_security_owner" in t07
    assert "GRANT " not in t07
    assert "REVOKE " not in t07


def test_p3e_fresh_database_harness_targets_typed_recovery_head() -> None:
    source = _source("scripts/ci/prepare_p3e_pg16.sh")
    assert "t07d8e9f0a34" in source
