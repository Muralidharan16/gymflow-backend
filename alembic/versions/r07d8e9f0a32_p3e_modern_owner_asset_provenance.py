"""P3E: preserve legacy branding actors and add modern owner provenance.

Revision ID: r07d8e9f0a32
Revises: q07d8e9f0a31
Create Date: 2026-08-16

Branding was created against the legacy ``gym_owners`` identity domain, while
the active owner session is backed by ``owners``. Reusing the legacy UUID
columns for modern owner IDs violates their foreign keys and would destroy the
meaning of historical rows. Keep the legacy columns/FKs intact, add explicit
modern-owner provenance columns, and replace only the bounded P3E finalize and
delete capabilities so current owner activity records the correct identity
domain.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "r07d8e9f0a32"
down_revision = "q07d8e9f0a31"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E branding provenance migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role(session_user, :role, 'SET')"),
        {"role": _SECURITY_OWNER},
    ).scalar_one():
        raise RuntimeError("migration_owner lacks bounded SET to app_security_owner")


def _fk_target(bind, constraint_name: str) -> tuple[str, str, str, str] | None:
    row = bind.execute(sa.text("""
        SELECT source_relation.relname::text AS source_table,
               source_attribute.attname::text AS source_column,
               target_relation.relname::text AS target_table,
               target_attribute.attname::text AS target_column
        FROM pg_catalog.pg_constraint AS constraint_data
        JOIN pg_catalog.pg_class AS source_relation
          ON source_relation.oid = constraint_data.conrelid
        JOIN pg_catalog.pg_namespace AS source_namespace
          ON source_namespace.oid = source_relation.relnamespace
        JOIN pg_catalog.pg_class AS target_relation
          ON target_relation.oid = constraint_data.confrelid
        JOIN pg_catalog.pg_namespace AS target_namespace
          ON target_namespace.oid = target_relation.relnamespace
        JOIN pg_catalog.pg_attribute AS source_attribute
          ON source_attribute.attrelid = source_relation.oid
         AND source_attribute.attnum = constraint_data.conkey[1]
        JOIN pg_catalog.pg_attribute AS target_attribute
          ON target_attribute.attrelid = target_relation.oid
         AND target_attribute.attnum = constraint_data.confkey[1]
        WHERE source_namespace.nspname = 'public'
          AND target_namespace.nspname = 'public'
          AND constraint_data.conname = :constraint_name
          AND constraint_data.contype = 'f'
          AND constraint_data.confdeltype = 'n'
          AND pg_catalog.array_length(constraint_data.conkey, 1) = 1
          AND pg_catalog.array_length(constraint_data.confkey, 1) = 1
    """), {"constraint_name": constraint_name}).one_or_none()
    return tuple(row) if row is not None else None


def _column_exists(bind, table: str, column: str) -> bool:
    return bool(bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS attribute
            WHERE attribute.attrelid = pg_catalog.to_regclass(:relation)
              AND attribute.attname = :column
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
        )
    """), {"relation": f"public.{table}", "column": column}).scalar_one())


def _function_contract(bind, name: str) -> dict[str, object]:
    row = bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               pg_catalog.has_function_privilege(
                   'app_runtime', procedure.oid, 'EXECUTE'
               ) AS api_execute,
               pg_catalog.has_function_privilege(
                   'worker_runtime', procedure.oid, 'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   'auth_runtime', procedure.oid, 'EXECUTE'
               ) AS auth_execute,
               pg_catalog.has_function_privilege(
                   'lifecycle_maintenance_runtime', procedure.oid, 'EXECUTE'
               ) AS maintenance_execute,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )
                   ) AS acl
                   WHERE acl.grantee = 0
                     AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute,
               pg_catalog.pg_get_functiondef(procedure.oid) AS definition
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = :name
          AND procedure.prokind = 'f'
    """), {"name": name}).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"P3E asset capability is missing: {name}")
    return dict(row)


def _require_capability_contract(bind) -> None:
    expected = {
        "finalize_organization_asset_job": (False, True),
        "delete_current_organization_asset": (True, False),
    }
    for name, (api_allowed, worker_allowed) in expected.items():
        row = _function_contract(bind, name)
        if (
            row["owner_name"] != _SECURITY_OWNER
            or not bool(row["prosecdef"])
            or row["volatility"] != "v"
            or set(row["proconfig"] or [])
            != {"search_path=pg_catalog", "row_security=on"}
            or bool(row["api_execute"]) != api_allowed
            or bool(row["worker_execute"]) != worker_allowed
            or bool(row["auth_execute"])
            or bool(row["maintenance_execute"])
            or bool(row["public_execute"])
        ):
            raise RuntimeError(f"P3E asset capability contract drift: {name}")


def _require_predecessor(bind) -> None:
    _require_capability_contract(bind)
    expected_legacy = {
        "organizations_logo_updated_by_fkey":
            ("organizations", "logo_updated_by", "gym_owners", "id"),
        "organizations_cover_updated_by_fkey":
            ("organizations", "cover_updated_by", "gym_owners", "id"),
        "organization_asset_audit_changed_by_fkey":
            ("organization_asset_audit", "changed_by", "gym_owners", "id"),
    }
    for name, expected in expected_legacy.items():
        if _fk_target(bind, name) != expected:
            raise RuntimeError(f"legacy branding actor FK drift: {name}")
    for table, column in (
        ("organizations", "logo_updated_by_owner_id"),
        ("organizations", "cover_updated_by_owner_id"),
        ("organization_asset_audit", "changed_by_owner_id"),
    ):
        if _column_exists(bind, table, column):
            raise RuntimeError(f"modern branding actor column already exists: {table}.{column}")


def _require_forward(bind) -> None:
    _require_capability_contract(bind)
    expected = {
        "organizations_logo_updated_by_fkey":
            ("organizations", "logo_updated_by", "gym_owners", "id"),
        "organizations_cover_updated_by_fkey":
            ("organizations", "cover_updated_by", "gym_owners", "id"),
        "organization_asset_audit_changed_by_fkey":
            ("organization_asset_audit", "changed_by", "gym_owners", "id"),
        "organizations_logo_updated_by_owner_fkey":
            ("organizations", "logo_updated_by_owner_id", "owners", "id"),
        "organizations_cover_updated_by_owner_fkey":
            ("organizations", "cover_updated_by_owner_id", "owners", "id"),
        "organization_asset_audit_changed_by_owner_fkey":
            ("organization_asset_audit", "changed_by_owner_id", "owners", "id"),
    }
    for name, target in expected.items():
        if _fk_target(bind, name) != target:
            raise RuntimeError(f"branding actor FK drift: {name}")
    definition = str(_function_contract(
        bind, "finalize_organization_asset_job"
    )["definition"])
    if (
        "logo_updated_by_owner_id = v_job.requested_by_owner_id" not in definition
        or "cover_updated_by_owner_id = v_job.requested_by_owner_id" not in definition
        or "changed_by_owner_id" not in definition
        or "logo_updated_by = NULL" not in definition
        or "cover_updated_by = NULL" not in definition
    ):
        raise RuntimeError("modern owner provenance is not bound in asset finalization")
    delete_definition = str(_function_contract(
        bind, "delete_current_organization_asset"
    )["definition"])
    if (
        "logo_updated_by_owner_id = v_owner_id" not in delete_definition
        or "cover_updated_by_owner_id = v_owner_id" not in delete_definition
        or "changed_by_owner_id" not in delete_definition
    ):
        raise RuntimeError("modern owner provenance is not bound in asset deletion")


def _replace_finalize(*, modern_owner: bool) -> None:
    logo_actor = (
        "logo_updated_by = NULL, "
        "logo_updated_by_owner_id = v_job.requested_by_owner_id,"
        if modern_owner
        else "logo_updated_by = v_job.requested_by_owner_id,"
    )
    cover_actor = (
        "cover_updated_by = NULL, "
        "cover_updated_by_owner_id = v_job.requested_by_owner_id,"
        if modern_owner
        else "cover_updated_by = v_job.requested_by_owner_id,"
    )
    audit_columns = (
        "id, org_id, changed_by_owner_id, asset_type, old_s3_key, "
        "new_s3_key, action, action_detail, ip_address"
        if modern_owner
        else "id, org_id, changed_by, asset_type, old_s3_key, "
        "new_s3_key, action, action_detail, ip_address"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(sa.text(r"""
        CREATE OR REPLACE FUNCTION app_secure.finalize_organization_asset_job(
            p_job_id uuid,
            p_lease_token uuid,
            p_width integer,
            p_height integer,
            p_size_bytes bigint,
            p_content_type text
        ) RETURNS TABLE (applied boolean, old_keys text[])
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_job record;
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_upload_hex text;
            v_original text;
            v_small text;
            v_medium text;
            v_large text;
            v_old_keys text[];
            v_old_primary text;
        BEGIN
            IF p_lease_token IS NULL OR p_width < 1 OR p_height < 1
               OR p_size_bytes < 1
               OR p_content_type NOT IN ('image/png', 'image/jpeg', 'image/webp') THEN
                RAISE EXCEPTION 'invalid organization asset finalization'
                    USING ERRCODE = '22023';
            END IF;
            SELECT job.* INTO v_job
            FROM public.organization_asset_jobs AS job
            WHERE job.id = p_job_id FOR UPDATE;
            IF NOT FOUND OR v_job.status <> 'processing'
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= v_now THEN
                applied := false; old_keys := ARRAY[]::text[]; RETURN NEXT; RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_job.requested_by_owner_id
                  AND owner_row.org_id = v_job.organization_id
            ) THEN
                UPDATE public.organization_asset_jobs
                SET status = 'cancelled', failure_code = 'owner_membership_revoked',
                    lease_token = NULL, lease_expires_at = NULL, updated_at = v_now
                WHERE id = p_job_id;
                applied := false; old_keys := ARRAY[]::text[]; RETURN NEXT; RETURN;
            END IF;

            v_upload_hex := pg_catalog.replace(v_job.upload_id::text, '-', '');
            v_original := 'originals/' || v_job.organization_id::text || '/' ||
                          v_upload_hex || '_original';
            IF v_job.asset_type = 'logo' THEN
                v_small := 'logos/' || v_job.organization_id::text || '/' ||
                           v_upload_hex || '_thumb.webp';
                v_medium := 'logos/' || v_job.organization_id::text || '/' ||
                            v_upload_hex || '_medium.webp';
                v_large := 'logos/' || v_job.organization_id::text || '/' ||
                           v_upload_hex || '_full.webp';
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.logo_key, organization.logo_thumb_key,
                    organization.logo_medium_key, organization.logo_full_key
                ]::text[], NULL), organization.logo_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_job.organization_id;
                UPDATE public.organizations
                SET logo_key = v_original, logo_thumb_key = v_small,
                    logo_medium_key = v_medium, logo_full_key = v_large,
                    logo_status = 'ready',
                    logo_meta = pg_catalog.jsonb_build_object(
                        'width', p_width, 'height', p_height,
                        'size_bytes', p_size_bytes, 'mime_type', p_content_type
                    ),
                    logo_updated_at = v_now,
                    __LOGO_ACTOR__
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            ELSE
                v_small := 'covers/' || v_job.organization_id::text || '/' ||
                           v_upload_hex || '_mobile.webp';
                v_medium := 'covers/' || v_job.organization_id::text || '/' ||
                            v_upload_hex || '_tablet.webp';
                v_large := 'covers/' || v_job.organization_id::text || '/' ||
                           v_upload_hex || '_desktop.webp';
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.cover_key, organization.cover_mobile_key,
                    organization.cover_tablet_key, organization.cover_desktop_key
                ]::text[], NULL), organization.cover_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_job.organization_id;
                UPDATE public.organizations
                SET cover_key = v_original, cover_mobile_key = v_small,
                    cover_tablet_key = v_medium, cover_desktop_key = v_large,
                    cover_status = 'ready',
                    cover_meta = pg_catalog.jsonb_build_object(
                        'width', p_width, 'height', p_height,
                        'size_bytes', p_size_bytes, 'mime_type', p_content_type,
                        'focal_point_y', v_job.focal_y
                    ),
                    cover_updated_at = v_now,
                    __COVER_ACTOR__
                    updated_at = v_now
                WHERE id = v_job.organization_id;
            END IF;
            INSERT INTO public.organization_asset_audit (
                __AUDIT_COLUMNS__
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_job.organization_id,
                v_job.requested_by_owner_id, v_job.asset_type,
                v_old_primary, v_original, 'uploaded',
                pg_catalog.jsonb_build_object(
                    'source', 'p3e_fenced_worker', 'job_id', p_job_id,
                    'attempt_count', v_job.attempt_count
                ), v_job.request_ip
            );
            UPDATE public.organization_asset_jobs
            SET status = 'completed', lease_token = NULL, lease_expires_at = NULL,
                failure_code = NULL, completed_at = v_now, updated_at = v_now
            WHERE id = p_job_id;
            applied := true;
            old_keys := COALESCE(v_old_keys, ARRAY[]::text[]);
            RETURN NEXT;
        END;
        $function$;
    """.replace("__LOGO_ACTOR__", logo_actor)
       .replace("__COVER_ACTOR__", cover_actor)
       .replace("__AUDIT_COLUMNS__", audit_columns)))
    op.execute("RESET ROLE")


def _replace_delete(*, modern_owner: bool) -> None:
    logo_actor = (
        "logo_updated_by = NULL, logo_updated_by_owner_id = v_owner_id,"
        if modern_owner
        else "logo_updated_by = v_owner_id,"
    )
    cover_actor = (
        "cover_updated_by = NULL, cover_updated_by_owner_id = v_owner_id,"
        if modern_owner
        else "cover_updated_by = v_owner_id,"
    )
    audit_columns = (
        "id, org_id, changed_by_owner_id, asset_type, old_s3_key, "
        "action, action_detail"
        if modern_owner
        else "id, org_id, changed_by, asset_type, old_s3_key, "
        "action, action_detail"
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(sa.text(r"""
        CREATE OR REPLACE FUNCTION app_secure.delete_current_organization_asset(
            p_asset_type text
        ) RETURNS text[]
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog SET row_security = on
        AS $function$
        DECLARE
            v_org_text text;
            v_user_text text;
            v_principal_type text;
            v_role text;
            v_gym text;
            v_org_id uuid;
            v_owner_id uuid;
            v_now timestamptz := pg_catalog.clock_timestamp();
            v_old_keys text[];
            v_old_primary text;
        BEGIN
            v_org_text := pg_catalog.current_setting('app.current_org_id', true);
            v_user_text := pg_catalog.current_setting('app.current_user_id', true);
            v_principal_type := pg_catalog.current_setting(
                'app.current_principal_type', true
            );
            v_role := pg_catalog.current_setting('app.current_role', true);
            v_gym := pg_catalog.current_setting('app.current_gym_id', true);
            IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = ''
               OR v_user_text IS NULL OR pg_catalog.btrim(v_user_text) = ''
               OR v_principal_type IS DISTINCT FROM 'owner'
               OR v_role IS DISTINCT FROM 'owner'
               OR (v_gym IS NOT NULL AND pg_catalog.btrim(v_gym) <> '')
               OR p_asset_type NOT IN ('logo', 'cover') THEN
                RAISE EXCEPTION 'organization asset owner context is required'
                    USING ERRCODE = '42501';
            END IF;
            BEGIN
                v_org_id := v_org_text::uuid;
                v_owner_id := v_user_text::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'invalid organization asset principal context'
                    USING ERRCODE = '42501';
            END;
            IF NOT EXISTS (
                SELECT 1 FROM public.owners AS owner_row
                WHERE owner_row.id = v_owner_id AND owner_row.org_id = v_org_id
            ) THEN
                RAISE EXCEPTION 'current owner membership is not authoritative'
                    USING ERRCODE = '42501';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(v_org_id::text || ':' || p_asset_type, 0)
            );
            UPDATE public.organization_asset_jobs AS job
            SET status = 'cancelled', lease_token = NULL, lease_expires_at = NULL,
                failure_code = 'asset_deleted', updated_at = v_now
            WHERE job.organization_id = v_org_id
              AND job.asset_type = p_asset_type
              AND job.status IN ('pending', 'processing');

            IF p_asset_type = 'logo' THEN
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.logo_key, organization.logo_thumb_key,
                    organization.logo_medium_key, organization.logo_full_key
                ]::text[], NULL), organization.logo_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_org_id;
                UPDATE public.organizations
                SET logo_key = NULL, logo_thumb_key = NULL,
                    logo_medium_key = NULL, logo_full_key = NULL,
                    logo_meta = NULL, logo_status = NULL,
                    logo_updated_at = v_now, __LOGO_ACTOR__
                    updated_at = v_now
                WHERE id = v_org_id;
            ELSE
                SELECT pg_catalog.array_remove(ARRAY[
                    organization.cover_key, organization.cover_mobile_key,
                    organization.cover_tablet_key, organization.cover_desktop_key
                ]::text[], NULL), organization.cover_key
                INTO v_old_keys, v_old_primary
                FROM public.organizations AS organization
                WHERE organization.id = v_org_id;
                UPDATE public.organizations
                SET cover_key = NULL, cover_mobile_key = NULL,
                    cover_tablet_key = NULL, cover_desktop_key = NULL,
                    cover_meta = NULL, cover_status = NULL,
                    cover_updated_at = v_now, __COVER_ACTOR__
                    updated_at = v_now
                WHERE id = v_org_id;
            END IF;

            IF v_old_primary IS NOT NULL THEN
                INSERT INTO public.organization_asset_audit (
                    __AUDIT_COLUMNS__
                ) VALUES (
                    pg_catalog.gen_random_uuid(), v_org_id, v_owner_id,
                    p_asset_type, v_old_primary, 'deleted',
                    pg_catalog.jsonb_build_object('source', 'p3e_bounded_delete')
                );
            END IF;
            RETURN COALESCE(v_old_keys, ARRAY[]::text[]);
        END;
        $function$;
    """.replace("__LOGO_ACTOR__", logo_actor)
       .replace("__COVER_ACTOR__", cover_actor)
       .replace("__AUDIT_COLUMNS__", audit_columns)))
    op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.add_column(
        "organizations",
        sa.Column("logo_updated_by_owner_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("cover_updated_by_owner_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "organization_asset_audit",
        sa.Column("changed_by_owner_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "organizations_logo_updated_by_owner_fkey",
        "organizations", "owners",
        ["logo_updated_by_owner_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "organizations_cover_updated_by_owner_fkey",
        "organizations", "owners",
        ["cover_updated_by_owner_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "organization_asset_audit_changed_by_owner_fkey",
        "organization_asset_audit", "owners",
        ["changed_by_owner_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_organization_asset_audit_single_actor_domain",
        "organization_asset_audit",
        "changed_by IS NULL OR changed_by_owner_id IS NULL",
    )

    op.execute("""
        GRANT UPDATE (
            logo_updated_by_owner_id, cover_updated_by_owner_id
        ) ON TABLE public.organizations TO app_security_owner
    """)
    op.execute("""
        GRANT INSERT (changed_by_owner_id)
        ON TABLE public.organization_asset_audit TO app_security_owner
    """)

    _replace_finalize(modern_owner=True)
    _replace_delete(modern_owner=True)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    _replace_finalize(modern_owner=False)
    _replace_delete(modern_owner=False)

    op.execute("""
        REVOKE UPDATE (
            logo_updated_by_owner_id, cover_updated_by_owner_id
        ) ON TABLE public.organizations FROM app_security_owner
    """)
    op.execute("""
        REVOKE INSERT (changed_by_owner_id)
        ON TABLE public.organization_asset_audit FROM app_security_owner
    """)
    op.drop_constraint(
        "ck_organization_asset_audit_single_actor_domain",
        "organization_asset_audit",
        type_="check",
    )
    op.drop_constraint(
        "organization_asset_audit_changed_by_owner_fkey",
        "organization_asset_audit",
        type_="foreignkey",
    )
    op.drop_constraint(
        "organizations_cover_updated_by_owner_fkey",
        "organizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "organizations_logo_updated_by_owner_fkey",
        "organizations",
        type_="foreignkey",
    )
    op.drop_column("organization_asset_audit", "changed_by_owner_id")
    op.drop_column("organizations", "cover_updated_by_owner_id")
    op.drop_column("organizations", "logo_updated_by_owner_id")
    _require_predecessor(bind)
