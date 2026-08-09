"""RBAC Hardening Phase 8 — complete branch_staff_roles contract.

Completes the 0024/0025 expand path without weakening tenant isolation.
The revision journals the exact predecessor contracts needed for a fail-closed
reversible downgrade, performs cross-tenant representation conversion only in
transaction-local owner maintenance windows, and replaces runtime functions
that otherwise depend on the legacy representation.

Revision ID: 0029_rbac_p8_contract
Revises: 0028_rbac_p7_role_events
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "0029_rbac_p8_contract"
down_revision = "0028_rbac_p7_role_events"
branch_labels = None
depends_on = None


_STATE_TABLE = "app_private.migration_0029_contract_state"
_GRANT_TABLE = "app_private.migration_0029_added_grants"

_FUNCTION_SIGNATURES = (
    "app_private.mark_snapshot_stale()",
    "app_private.compile_member_permissions(uuid,uuid,uuid,smallint)",
    "app_private.handle_user_deactivation_cascade()",
    "app_private.log_branch_staff_role_audit()",
    "app_private.sync_branch_staff_role_contract_fields()",
)
_FUNCTION_OWNERS = {
    "app_private.mark_snapshot_stale()": "app_security_owner",
    "app_private.compile_member_permissions(uuid,uuid,uuid,smallint)": "app_security_owner",
    "app_private.handle_user_deactivation_cascade()": "app_rls_executor",
    "app_private.log_branch_staff_role_audit()": "app_rls_executor",
    "app_private.sync_branch_staff_role_contract_fields()": "migration_owner",
}
_LEGACY_CONSTRAINTS = (
    "fk_branch_staff_assigned_by",
    "fk_branch_staff_revoked_by",
    "fk_branch_staff_user_org",
    "exclude_overlapping_staff_assignments",
)
_EXPAND_CONSTRAINTS = (
    "fk_bsr_member_id",
    "fk_bsr_member_org",
    "fk_bsr_role_id",
    "fk_bsr_scope_type_id",
)
_LEGACY_INDEXES = (
    "ix_branch_staff_user_active",
    "ix_branch_staff_branch_active",
)
_INTERNAL_POLICIES = (
    "rbac_internal_staff_roles_select",
    "rbac_internal_staff_roles_update",
)
_ADDED_GRANT_ALLOWLIST = {
    ("table", "public.organization_members", "app_security_owner", "SELECT"),
    ("table", "public.organization_members", "app_security_owner", "UPDATE"),
    ("table", "public.member_permission_snapshots", "app_security_owner", "SELECT"),
    ("table", "public.member_permission_snapshots", "app_security_owner", "UPDATE"),
    ("table", "public.permissions", "app_security_owner", "SELECT"),
    ("table", "public.organization_members", "app_rls_executor", "SELECT"),
    ("table", "public.organization_users", "app_rls_executor", "SELECT"),
    ("table", "public.staff_roles", "app_rls_executor", "SELECT"),
    ("table", "public.branch_staff_roles", "app_rls_executor", "SELECT"),
    ("table", "public.branch_staff_roles", "app_rls_executor", "UPDATE"),
    ("table", "public.branch_audit_log", "app_rls_executor", "INSERT"),
    ("sequence", "public.branch_audit_log_seq", "app_rls_executor", "USAGE"),
    ("schema", "app_private", "app_runtime", "USAGE"),
}


def _bind():
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("0029 requires an online Alembic connection.")
    return bind


def _identity(bind):
    return dict(
        bind.execute(
            sa.text(
                """
                SELECT
                    session_user::text AS session_user_name,
                    current_user::text AS current_user_name
                """
            )
        ).mappings().one()
    )


def _require_migration_owner(bind):
    observed = _identity(bind)
    expected = {
        "session_user_name": "migration_owner",
        "current_user_name": "migration_owner",
    }
    if observed != expected:
        raise RuntimeError(
            f"0029 requires migration_owner identity; observed {observed!r}."
        )


def _can_set_role(bind, role_name):
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "session_user, CAST(:role_name AS name), 'SET')"
            ),
            {"role_name": role_name},
        ).scalar_one()
    )


def _schema_privilege(bind, role_name, schema_name, privilege):
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role_name, :schema_name, :privilege)"
            ),
            {
                "role_name": role_name,
                "schema_name": schema_name,
                "privilege": privilege,
            },
        ).scalar_one()
    )


def _public_schema_privilege(bind, schema_name, privilege):
    row = bind.execute(
        sa.text(
            """
            SELECT
                count(DISTINCT namespace_data.oid)::int AS schema_count,
                COALESCE(
                    bool_or(
                        acl_data.grantee = 0
                        AND acl_data.privilege_type = :privilege
                    ),
                    FALSE
                ) AS public_has_privilege
            FROM pg_catalog.pg_namespace AS namespace_data
            LEFT JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    namespace_data.nspacl,
                    pg_catalog.acldefault(
                        'n'::"char",
                        namespace_data.nspowner
                    )
                )
            ) AS acl_data ON TRUE
            WHERE namespace_data.nspname = :schema_name
            """
        ),
        {
            "schema_name": schema_name,
            "privilege": privilege.upper(),
        },
    ).mappings().one()
    if row["schema_count"] != 1:
        raise RuntimeError(
            f"Required schema {schema_name!r} is absent or ambiguous."
        )
    return bool(row["public_has_privilege"])


def _reject_public_private_create(bind):
    if _public_schema_privilege(bind, "app_private", "CREATE"):
        raise RuntimeError("PUBLIC CREATE on app_private is forbidden.")


def _require_role_foundation(bind):
    _reject_public_private_create(bind)
    for role_name in ("app_security_owner", "app_rls_executor"):
        if not _can_set_role(bind, role_name):
            raise RuntimeError(
                f"migration_owner cannot SET ROLE {role_name}."
            )
        for schema_name in ("app_private", "public"):
            if not _schema_privilege(
                bind, role_name, schema_name, "USAGE"
            ):
                raise RuntimeError(
                    f"{role_name} lacks USAGE on schema {schema_name}."
                )
    owner = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(c.relowner)::text
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            WHERE n.nspname = 'app_secure'
              AND c.relname = 'v_active_branch_staff_roles'
              AND c.relkind = 'v'
            """
        )
    ).scalar_one()
    if owner != "app_security_owner":
        raise RuntimeError(
            "Predecessor active-role view is not owned by app_security_owner."
        )


def _prepare_private_create(bind, role_name):
    _reject_public_private_create(bind)
    if _schema_privilege(bind, role_name, "app_private", "CREATE"):
        return False
    bind.exec_driver_sql(
        f"GRANT CREATE ON SCHEMA app_private TO {role_name}"
    )
    if not _schema_privilege(bind, role_name, "app_private", "CREATE"):
        raise RuntimeError(
            f"Failed to add bounded CREATE on app_private for {role_name}."
        )
    return True


def _release_private_create(bind, role_name, added):
    if added:
        bind.exec_driver_sql(
            f"REVOKE CREATE ON SCHEMA app_private FROM {role_name}"
        )
        if _schema_privilege(bind, role_name, "app_private", "CREATE"):
            raise RuntimeError(
                f"Bounded CREATE on app_private leaked for {role_name}."
            )
    _reject_public_private_create(bind)


def _run_as_role(bind, role_name, sql):
    if role_name not in {"app_security_owner", "app_rls_executor"}:
        raise RuntimeError(f"Unsupported bounded role {role_name!r}.")
    _require_migration_owner(bind)
    if not _can_set_role(bind, role_name):
        raise RuntimeError(
            f"migration_owner cannot SET ROLE {role_name}."
        )
    bind.exec_driver_sql(f"SET LOCAL ROLE {role_name}")
    try:
        current = _identity(bind)
        if current["session_user_name"] != "migration_owner":
            raise RuntimeError("SET LOCAL ROLE changed session_user.")
        if current["current_user_name"] != role_name:
            raise RuntimeError(
                f"Failed to enter bounded role {role_name}."
            )
        bind.exec_driver_sql(sql)
    finally:
        bind.exec_driver_sql("RESET ROLE")
    _require_migration_owner(bind)


def _relation_security(bind, qualified):
    return dict(
        bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_userbyid(c.relowner)::text AS owner_name,
                    c.relrowsecurity AS rls_enabled,
                    c.relforcerowsecurity AS rls_forced
                FROM pg_catalog.pg_class AS c
                WHERE c.oid = CAST(:qualified AS regclass)
                """
            ),
            {"qualified": qualified},
        ).mappings().one()
    )


def _require_forced_owner_tables(bind):
    expected = {
        "owner_name": "migration_owner",
        "rls_enabled": True,
        "rls_forced": True,
    }
    for relation in (
        "public.branch_staff_roles",
        "public.organization_members",
    ):
        state = _relation_security(bind, relation)
        if state != expected:
            raise RuntimeError(
                f"0029 security contract drift for {relation}: {state!r}."
            )


def _require_forced_owner_organization_users(bind):
    expected = {
        "owner_name": "migration_owner",
        "rls_enabled": True,
        "rls_forced": True,
    }
    relation = "public.organization_users"
    state = _relation_security(bind, relation)
    if state != expected:
        raise RuntimeError(
            f"0029 security contract drift for {relation}: {state!r}."
        )


def _trigger_state(bind, name):
    row = bind.execute(
        sa.text(
            """
            SELECT t.tgenabled::text AS enabled_state, t.tgisinternal
            FROM pg_catalog.pg_trigger AS t
            WHERE t.tgrelid = 'public.branch_staff_roles'::regclass
              AND t.tgname = :name
            """
        ),
        {"name": name},
    ).mappings().one_or_none()
    if row is None or row["tgisinternal"]:
        raise RuntimeError(f"Expected user trigger {name!r} is absent.")
    return row["enabled_state"]


def _require_trigger_states(bind, expected):
    for name in (
        "trg_bsr_validate_rls_context",
        "trg_invalidate_perm_snapshot",
    ):
        observed = _trigger_state(bind, name)
        if observed != expected:
            raise RuntimeError(
                f"Unexpected trigger state {name}={observed!r}; "
                f"expected {expected!r}."
            )


def _require_audit_trigger_state(bind, expected):
    name = "trg_audit_branch_staff_roles"
    observed = _trigger_state(bind, name)
    if observed != expected:
        raise RuntimeError(
            f"Unexpected trigger state {name}={observed!r}; "
            f"expected {expected!r}."
        )


def _create_state_tables(bind):
    for qualified in (_STATE_TABLE, _GRANT_TABLE):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:name)"),
            {"name": qualified},
        ).scalar_one() is not None:
            raise RuntimeError(f"0029 marker collision: {qualified}.")

    op.execute("""
        CREATE TABLE app_private.migration_0029_contract_state (
            object_kind VARCHAR(32) NOT NULL,
            object_name TEXT NOT NULL,
            owner_name TEXT NULL,
            definition TEXT NOT NULL,
            acl_text TEXT NULL,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (object_kind, object_name)
        );
    """)
    op.execute("""
        REVOKE ALL ON TABLE
            app_private.migration_0029_contract_state
        FROM PUBLIC;
    """)
    op.execute("""
        CREATE TABLE app_private.migration_0029_added_grants (
            object_kind VARCHAR(16) NOT NULL,
            object_identity TEXT NOT NULL,
            grantee TEXT NOT NULL,
            privilege_type VARCHAR(16) NOT NULL,
            PRIMARY KEY (
                object_kind,
                object_identity,
                grantee,
                privilege_type
            )
        );
    """)
    op.execute("""
        REVOKE ALL ON TABLE
            app_private.migration_0029_added_grants
        FROM PUBLIC;
    """)


def _capture_function_state(bind):
    for signature in _FUNCTION_SIGNATURES:
        row = bind.execute(
            sa.text(
                """
                SELECT
                    p.oid::regprocedure::text AS resolved_signature,
                    pg_catalog.pg_get_userbyid(p.proowner)::text AS owner_name,
                    pg_catalog.pg_get_functiondef(p.oid) AS definition,
                    p.proacl::text AS acl_text
                FROM pg_catalog.pg_proc AS p
                WHERE p.oid = pg_catalog.to_regprocedure(:signature)
                """
            ),
            {"signature": signature},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(
                f"0029 predecessor function absent: {signature}."
            )
        expected_owner = _FUNCTION_OWNERS[signature]
        if row["owner_name"] != expected_owner:
            raise RuntimeError(
                f"Unexpected predecessor owner for {signature}: "
                f"{row['owner_name']!r}."
            )
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.migration_0029_contract_state (
                    object_kind,
                    object_name,
                    owner_name,
                    definition,
                    acl_text
                ) VALUES (
                    'function',
                    :object_name,
                    :owner_name,
                    :definition,
                    :acl_text
                )
                """
            ),
            {
                "object_name": signature,
                "owner_name": row["owner_name"],
                "definition": row["definition"],
                "acl_text": row["acl_text"],
            },
        )


def _capture_view_state(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT
                c.oid::oid AS relation_oid,
                pg_catalog.pg_get_userbyid(c.relowner)::text AS owner_name,
                pg_catalog.pg_get_viewdef(c.oid, true) AS definition,
                c.relacl::text AS acl_text,
                COALESCE(to_jsonb(c.reloptions), '[]'::jsonb) AS reloptions,
                pg_catalog.obj_description(c.oid, 'pg_class') AS comment_text,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            c.relacl,
                            pg_catalog.acldefault(
                                'r'::"char",
                                c.relowner
                            )
                        )
                    ) AS acl_data
                    WHERE acl_data.grantee = 0
                      AND acl_data.privilege_type = 'SELECT'
                ) AS public_select
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            WHERE n.nspname = 'app_secure'
              AND c.relname = 'v_active_branch_staff_roles'
              AND c.relkind = 'v'
            """
        )
    ).mappings().one()
    if row["owner_name"] != "app_security_owner":
        raise RuntimeError("Unexpected predecessor active-role view owner.")
    options = set(row["reloptions"] or [])
    if options != {"security_barrier=true", "security_invoker=true"}:
        raise RuntimeError(
            f"Unexpected predecessor active-role view options: {options!r}."
        )
    if row["public_select"]:
        raise RuntimeError("PUBLIC SELECT on predecessor active-role view.")
    for role_name in ("app_runtime", "readonly_analytics"):
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege("
                ":role_name, CAST(:relation_oid AS oid), 'SELECT')"
            ),
            {
                "role_name": role_name,
                "relation_oid": row["relation_oid"],
            },
        ).scalar_one():
            raise RuntimeError(
                f"{role_name} lacks predecessor active-role view SELECT."
            )

    bind.execute(
        sa.text(
            """
            INSERT INTO app_private.migration_0029_contract_state (
                object_kind,
                object_name,
                owner_name,
                definition,
                acl_text,
                metadata_json
            ) VALUES (
                'view',
                'app_secure.v_active_branch_staff_roles',
                :owner_name,
                :definition,
                :acl_text,
                jsonb_build_object(
                    'reloptions', CAST(:reloptions AS jsonb),
                    'comment', CAST(:comment_text AS text)
                )
            )
            """
        ),
        {
            "owner_name": row["owner_name"],
            "definition": row["definition"],
            "acl_text": row["acl_text"],
            "reloptions": json.dumps(row["reloptions"] or []),
            "comment_text": row["comment_text"],
        },
    )


def _capture_constraint_state(bind):
    for constraint_name in _LEGACY_CONSTRAINTS + _EXPAND_CONSTRAINTS:
        row = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_constraintdef(con.oid, true) AS definition,
                    con.convalidated AS validated
                FROM pg_catalog.pg_constraint AS con
                WHERE con.conrelid = 'public.branch_staff_roles'::regclass
                  AND con.conname = :constraint_name
                """
            ),
            {"constraint_name": constraint_name},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(
                f"0029 predecessor constraint absent: {constraint_name}."
            )
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.migration_0029_contract_state (
                    object_kind,
                    object_name,
                    definition,
                    metadata_json
                ) VALUES (
                    'constraint',
                    :name,
                    :definition,
                    jsonb_build_object(
                        'validated',
                        CAST(:validated AS boolean)
                    )
                )
                """
            ),
            {
                "name": constraint_name,
                "definition": row["definition"],
                "validated": row["validated"],
            },
        )


def _capture_index_state(bind):
    for index_name in _LEGACY_INDEXES:
        definition = bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_get_indexdef(c.oid)
                FROM pg_catalog.pg_class AS c
                WHERE c.oid = pg_catalog.to_regclass(:qualified)
                """
            ),
            {"qualified": f"public.{index_name}"},
        ).scalar_one_or_none()
        if definition is None:
            raise RuntimeError(
                f"0029 predecessor index absent: {index_name}."
            )
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.migration_0029_contract_state (
                    object_kind, object_name, definition
                ) VALUES ('index', :name, :definition)
                """
            ),
            {"name": index_name, "definition": definition},
        )


def _capture_predecessor_state(bind):
    _capture_function_state(bind)
    _capture_view_state(bind)
    _capture_constraint_state(bind)
    _capture_index_state(bind)
    count = bind.execute(
        sa.text(
            "SELECT count(*) FROM app_private.migration_0029_contract_state"
        )
    ).scalar_one()
    expected = len(_FUNCTION_SIGNATURES) + 1 + len(_LEGACY_CONSTRAINTS) + len(_EXPAND_CONSTRAINTS) + len(_LEGACY_INDEXES)
    if count != expected:
        raise RuntimeError(
            f"0029 predecessor journal count {count}, expected {expected}."
        )


def _grant_if_needed(bind, object_kind, object_identity, grantee, privilege):
    key = (object_kind, object_identity, grantee, privilege)
    if key not in _ADDED_GRANT_ALLOWLIST:
        raise RuntimeError(f"Unapproved 0029 privilege request: {key!r}.")

    if object_kind == "table":
        has_privilege = bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_table_privilege("
                    ":grantee, :object_identity, :privilege)"
                ),
                {
                    "grantee": grantee,
                    "object_identity": object_identity,
                    "privilege": privilege,
                },
            ).scalar_one()
        )
        grant_sql = (
            f"GRANT {privilege} ON TABLE {object_identity} TO {grantee}"
        )
    elif object_kind == "sequence":
        has_privilege = bool(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_sequence_privilege("
                    ":grantee, :object_identity, :privilege)"
                ),
                {
                    "grantee": grantee,
                    "object_identity": object_identity,
                    "privilege": privilege,
                },
            ).scalar_one()
        )
        grant_sql = (
            f"GRANT {privilege} ON SEQUENCE {object_identity} TO {grantee}"
        )
    elif object_kind == "schema":
        has_privilege = _schema_privilege(
            bind, grantee, object_identity, privilege
        )
        grant_sql = (
            f"GRANT {privilege} ON SCHEMA {object_identity} TO {grantee}"
        )
    else:
        raise RuntimeError(f"Unsupported privilege object kind {object_kind!r}.")

    if not has_privilege:
        bind.exec_driver_sql(grant_sql)
        bind.execute(
            sa.text(
                """
                INSERT INTO app_private.migration_0029_added_grants (
                    object_kind,
                    object_identity,
                    grantee,
                    privilege_type
                ) VALUES (
                    :object_kind,
                    :object_identity,
                    :grantee,
                    :privilege
                )
                """
            ),
            {
                "object_kind": object_kind,
                "object_identity": object_identity,
                "grantee": grantee,
                "privilege": privilege,
            },
        )


def _prepare_runtime_authority(bind):
    for item in sorted(_ADDED_GRANT_ALLOWLIST):
        _grant_if_needed(bind, *item)

    op.execute("""
        CREATE POLICY rbac_internal_staff_roles_select
        ON public.branch_staff_roles
        FOR SELECT
        TO app_rls_executor, app_security_owner
        USING (
            org_id = CASE
                WHEN pg_catalog.pg_input_is_valid(
                    NULLIF(pg_catalog.current_setting(
                        'app.current_org_id', true
                    ), ''),
                    'uuid'
                )
                THEN CAST(NULLIF(pg_catalog.current_setting(
                    'app.current_org_id', true
                ), '') AS UUID)
                ELSE CAST(NULL AS UUID)
            END
            AND deleted_at IS NULL
        );
    """)

    op.execute("""
        CREATE POLICY rbac_internal_staff_roles_update
        ON public.branch_staff_roles
        FOR UPDATE
        TO app_rls_executor
        USING (
            org_id = CASE
                WHEN pg_catalog.pg_input_is_valid(
                    NULLIF(pg_catalog.current_setting(
                        'app.current_org_id', true
                    ), ''),
                    'uuid'
                )
                THEN CAST(NULLIF(pg_catalog.current_setting(
                    'app.current_org_id', true
                ), '') AS UUID)
                ELSE CAST(NULL AS UUID)
            END
            AND deleted_at IS NULL
        )
        WITH CHECK (
            org_id = CASE
                WHEN pg_catalog.pg_input_is_valid(
                    NULLIF(pg_catalog.current_setting(
                        'app.current_org_id', true
                    ), ''),
                    'uuid'
                )
                THEN CAST(NULLIF(pg_catalog.current_setting(
                    'app.current_org_id', true
                ), '') AS UUID)
                ELSE CAST(NULL AS UUID)
            END
            AND deleted_at IS NULL
        );
    """)


def _require_app_runtime_compile_authority(bind):
    if not _schema_privilege(
        bind, "app_runtime", "app_private", "USAGE"
    ):
        raise RuntimeError(
            "app_runtime lacks USAGE on app_private required to invoke "
            "compile_member_permissions."
        )
    if _schema_privilege(
        bind, "app_runtime", "app_private", "CREATE"
    ):
        raise RuntimeError(
            "app_runtime must not have CREATE on app_private."
        )
    if _public_schema_privilege(bind, "app_private", "USAGE"):
        raise RuntimeError("PUBLIC USAGE on app_private is forbidden.")
    if _public_schema_privilege(bind, "app_private", "CREATE"):
        raise RuntimeError("PUBLIC CREATE on app_private is forbidden.")

    can_execute = bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                "CAST(:role_name AS name), "
                "CAST(:signature AS text), "
                "'EXECUTE')"
            ),
            {
                "role_name": "app_runtime",
                "signature": (
                    "app_private.compile_member_permissions"
                    "(uuid,uuid,uuid,smallint)"
                ),
            },
        ).scalar_one()
    )
    if not can_execute:
        raise RuntimeError(
            "app_runtime lacks EXECUTE on "
            "app_private.compile_member_permissions."
        )


def _replace_runtime_functions(bind):
    added_security_create = _prepare_private_create(
        bind, "app_security_owner"
    )
    added_executor_create = _prepare_private_create(
        bind, "app_rls_executor"
    )
    try:
        _run_as_role(
            bind,
            "app_security_owner",
            r"""
            CREATE OR REPLACE FUNCTION app_private.mark_snapshot_stale()
            RETURNS TRIGGER
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            SET row_security = on
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                v_member_id UUID;
                v_org_id UUID;
                v_branch_id UUID;
                v_context_text TEXT;
                v_context_org UUID;
                v_member_rows INTEGER;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    v_member_id := OLD.organization_member_id;
                    v_org_id := OLD.org_id;
                    v_branch_id := OLD.branch_id;
                ELSE
                    v_member_id := NEW.organization_member_id;
                    v_org_id := NEW.org_id;
                    v_branch_id := NEW.branch_id;
                END IF;

                IF v_member_id IS NULL THEN
                    RETURN COALESCE(NEW, OLD);
                END IF;

                v_context_text := NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true),
                    ''
                );
                IF v_context_text IS NULL
                   OR NOT pg_catalog.pg_input_is_valid(v_context_text, 'uuid')
                THEN
                    RAISE EXCEPTION
                        'Snapshot invalidation requires valid app.current_org_id'
                        USING ERRCODE = '42501';
                END IF;
                v_context_org := v_context_text::UUID;

                IF v_org_id IS DISTINCT FROM v_context_org THEN
                    RAISE EXCEPTION
                        'Snapshot invalidation tenant mismatch'
                        USING ERRCODE = '42501';
                END IF;

                UPDATE public.member_permission_snapshots
                SET is_stale = TRUE,
                    updated_at = clock_timestamp()
                WHERE organization_member_id = v_member_id
                  AND org_id = v_org_id
                  AND (branch_id = v_branch_id OR branch_id IS NULL);

                UPDATE public.organization_members
                SET permission_version = permission_version + 1,
                    updated_at = clock_timestamp()
                WHERE id = v_member_id
                  AND org_id = v_org_id;

                GET DIAGNOSTICS v_member_rows = ROW_COUNT;
                IF v_member_rows <> 1 THEN
                    RAISE EXCEPTION
                        'Snapshot invalidation expected one organization member; got %',
                        v_member_rows;
                END IF;

                RETURN COALESCE(NEW, OLD);
            END;
            $function$;
            """,
        )

        _run_as_role(
            bind,
            "app_security_owner",
            r"""
            CREATE OR REPLACE FUNCTION app_private.compile_member_permissions(
                p_organization_member_id UUID,
                p_org_id UUID,
                p_branch_id UUID,
                p_scope_type_id SMALLINT DEFAULT 2
            )
            RETURNS JSONB
            STRICT
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            SET row_security = on
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                v_permission_codes JSONB;
                v_context_text TEXT;
                v_context_org UUID;
            BEGIN
                v_context_text := NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true),
                    ''
                );
                IF v_context_text IS NULL
                   OR NOT pg_catalog.pg_input_is_valid(v_context_text, 'uuid')
                THEN
                    RAISE EXCEPTION
                        'Permission compilation requires valid app.current_org_id'
                        USING ERRCODE = '42501';
                END IF;
                v_context_org := v_context_text::UUID;

                IF p_org_id IS DISTINCT FROM v_context_org THEN
                    RAISE EXCEPTION
                        'Permission compilation tenant mismatch'
                        USING ERRCODE = '42501';
                END IF;

                SELECT jsonb_agg(DISTINCT p.code ORDER BY p.code)
                INTO v_permission_codes
                FROM public.branch_staff_roles AS bsr
                JOIN public.effective_role_permissions AS erp
                  ON erp.role_id = bsr.role_id
                JOIN public.permissions AS p
                  ON p.id = erp.permission_id
                WHERE bsr.organization_member_id = p_organization_member_id
                  AND bsr.org_id = p_org_id
                  AND bsr.branch_id = p_branch_id
                  AND bsr.scope_type_id = p_scope_type_id
                  AND bsr.revoked_at IS NULL
                  AND bsr.deleted_at IS NULL
                  AND bsr.effective_from <= clock_timestamp()
                  AND (
                        bsr.effective_to IS NULL
                        OR bsr.effective_to > clock_timestamp()
                  );

                RETURN COALESCE(v_permission_codes, '[]'::jsonb);
            END;
            $function$;
            """,
        )

        _run_as_role(
            bind,
            "app_rls_executor",
            r"""
            CREATE OR REPLACE FUNCTION app_private.handle_user_deactivation_cascade()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path TO 'pg_catalog'
            SET row_security TO 'on'
            AS $function$
            DECLARE
                v_context_text TEXT;
                v_context_org UUID;
                v_actor_text TEXT;
                v_actor_user UUID;
                v_actor_member UUID;
                v_has_roles BOOLEAN;
            BEGIN
                v_context_text := NULLIF(
                    pg_catalog.current_setting('app.current_org_id', true),
                    ''
                );
                IF v_context_text IS NULL
                   OR NOT pg_catalog.pg_input_is_valid(v_context_text, 'uuid')
                THEN
                    RAISE EXCEPTION
                        'Deactivation requires valid app.current_org_id'
                        USING ERRCODE = '42501';
                END IF;
                v_context_org := v_context_text::UUID;

                IF NEW.org_id IS DISTINCT FROM v_context_org THEN
                    RAISE EXCEPTION
                        'Deactivation tenant mismatch'
                        USING ERRCODE = '42501';
                END IF;

                IF OLD.is_active = TRUE AND NEW.is_active = FALSE THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.branch_staff_roles AS bsr
                        JOIN public.organization_members AS target_member
                          ON target_member.id = bsr.organization_member_id
                         AND target_member.org_id = bsr.org_id
                        WHERE target_member.user_id = NEW.id
                          AND target_member.org_id = NEW.org_id
                          AND bsr.revoked_at IS NULL
                          AND bsr.deleted_at IS NULL
                    )
                    INTO v_has_roles;

                    IF v_has_roles THEN
                        v_actor_text := NULLIF(
                            pg_catalog.current_setting(
                                'app.current_user_id', true
                            ),
                            ''
                        );
                        IF v_actor_text IS NULL
                           OR NOT pg_catalog.pg_input_is_valid(
                               v_actor_text, 'uuid'
                           )
                        THEN
                            RAISE EXCEPTION
                                'Deactivation requires valid app.current_user_id'
                                USING ERRCODE = '42501';
                        END IF;
                        v_actor_user := v_actor_text::UUID;

                        SELECT om.id
                        INTO STRICT v_actor_member
                        FROM public.organization_members AS om
                        WHERE om.org_id = NEW.org_id
                          AND om.user_id = v_actor_user
                          AND om.deleted_at IS NULL;

                        UPDATE public.branch_staff_roles AS bsr
                        SET revoked_at = clock_timestamp(),
                            revoked_by = v_actor_member
                        FROM public.organization_members AS target_member
                        WHERE bsr.organization_member_id = target_member.id
                          AND bsr.org_id = target_member.org_id
                          AND target_member.user_id = NEW.id
                          AND target_member.org_id = NEW.org_id
                          AND bsr.revoked_at IS NULL
                          AND bsr.deleted_at IS NULL;
                    END IF;
                END IF;

                RETURN NEW;
            END;
            $function$;
            """,
        )

        _run_as_role(
            bind,
            "app_rls_executor",
            r"""
            CREATE OR REPLACE FUNCTION app_private.log_branch_staff_role_audit()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path TO 'pg_catalog'
            SET row_security TO 'on'
            AS $function$
            DECLARE
                v_actor_text TEXT;
                v_actor_user UUID;
                v_fallback_member UUID;
                v_fallback_user UUID;
                v_target_user UUID;
                v_role_code TEXT;
                v_audit_actor UUID;
                v_action TEXT;
                v_reason TEXT;
                v_diff JSONB;
            BEGIN
                SELECT om.user_id
                INTO STRICT v_target_user
                FROM public.organization_members AS om
                WHERE om.id = NEW.organization_member_id
                  AND om.org_id = NEW.org_id;

                SELECT sr.code
                INTO STRICT v_role_code
                FROM public.staff_roles AS sr
                WHERE sr.id = NEW.role_id;

                IF TG_OP = 'INSERT' THEN
                    v_action := 'staff_role_assigned';
                    v_reason := 'Staff role assigned to branch';
                    v_fallback_member := NEW.assigned_by;
                    v_diff := jsonb_build_object(
                        'role_assignment_id', NEW.id,
                        'organization_member_id', NEW.organization_member_id,
                        'user_id', v_target_user,
                        'role_id', NEW.role_id,
                        'role_code', v_role_code,
                        'effective_from', NEW.effective_from,
                        'effective_to', NEW.effective_to,
                        'assigned_by_member_id', NEW.assigned_by
                    );
                ELSIF TG_OP = 'UPDATE'
                      AND OLD.revoked_at IS NULL
                      AND NEW.revoked_at IS NOT NULL
                THEN
                    v_action := 'staff_role_revoked';
                    v_reason := 'Staff role assignment revoked';
                    v_fallback_member := NEW.revoked_by;
                    v_diff := jsonb_build_object(
                        'role_assignment_id', NEW.id,
                        'organization_member_id', NEW.organization_member_id,
                        'user_id', v_target_user,
                        'role_id', NEW.role_id,
                        'role_code', v_role_code,
                        'revoked_at', NEW.revoked_at,
                        'revoked_by_member_id', NEW.revoked_by
                    );
                ELSE
                    RETURN NEW;
                END IF;

                v_actor_text := NULLIF(
                    pg_catalog.current_setting('app.current_user_id', true),
                    ''
                );
                IF v_actor_text IS NOT NULL THEN
                    IF NOT pg_catalog.pg_input_is_valid(v_actor_text, 'uuid') THEN
                        RAISE EXCEPTION
                            'Audit actor app.current_user_id is malformed'
                            USING ERRCODE = '42501';
                    END IF;
                    v_actor_user := v_actor_text::UUID;

                    PERFORM 1
                    FROM public.organization_users AS ou
                    WHERE ou.id = v_actor_user
                      AND ou.org_id = NEW.org_id;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'Audit actor does not belong to assignment organization'
                            USING ERRCODE = '42501';
                    END IF;
                END IF;

                IF v_fallback_member IS NOT NULL THEN
                    SELECT om.user_id
                    INTO STRICT v_fallback_user
                    FROM public.organization_members AS om
                    WHERE om.id = v_fallback_member
                      AND om.org_id = NEW.org_id;
                END IF;

                v_audit_actor := COALESCE(v_actor_user, v_fallback_user);
                IF v_audit_actor IS NULL THEN
                    RAISE EXCEPTION
                        'Audit actor is required for branch staff role mutation';
                END IF;

                INSERT INTO public.branch_audit_log (
                    branch_id,
                    org_id,
                    actor_id,
                    action,
                    reason,
                    diff,
                    created_at
                ) VALUES (
                    NEW.branch_id,
                    NEW.org_id,
                    v_audit_actor,
                    v_action,
                    v_reason,
                    v_diff,
                    clock_timestamp()
                );

                RETURN NEW;
            END;
            $function$;
            """,
        )
    finally:
        _release_private_create(
            bind, "app_rls_executor", added_executor_create
        )
        _release_private_create(
            bind, "app_security_owner", added_security_create
        )


def _drop_secure_view(bind):
    _run_as_role(
        bind,
        "app_security_owner",
        "DROP VIEW app_secure.v_active_branch_staff_roles RESTRICT",
    )


def _create_post_contract_view(bind):
    _run_as_role(
        bind,
        "app_security_owner",
        r"""
        CREATE VIEW app_secure.v_active_branch_staff_roles
        WITH (
            security_barrier = true,
            security_invoker = true
        )
        AS
        SELECT
            bsr.id,
            bsr.org_id,
            bsr.branch_id,
            bsr.organization_member_id,
            bsr.role_id,
            sr.code AS role_code,
            sr.hierarchy_level,
            bsr.scope_type_id,
            st.code AS scope_code,
            bsr.assignment_source,
            bsr.assigned_by,
            bsr.assigned_at,
            bsr.effective_from,
            bsr.effective_to,
            om.user_id AS user_id,
            sr.code AS role_legacy,
            bsr.created_at
        FROM public.branch_staff_roles AS bsr
        JOIN public.organization_members AS om
          ON om.id = bsr.organization_member_id
         AND om.org_id = bsr.org_id
        JOIN public.staff_roles AS sr
          ON sr.id = bsr.role_id
        JOIN public.scope_types AS st
          ON st.id = bsr.scope_type_id
        WHERE bsr.deleted_at IS NULL
          AND bsr.revoked_at IS NULL
        """,
    )
    _run_as_role(
        bind,
        "app_security_owner",
        "REVOKE ALL ON app_secure.v_active_branch_staff_roles FROM PUBLIC",
    )
    _run_as_role(
        bind,
        "app_security_owner",
        "GRANT SELECT ON app_secure.v_active_branch_staff_roles "
        "TO app_runtime, readonly_analytics",
    )
    _run_as_role(
        bind,
        "app_security_owner",
        "COMMENT ON VIEW app_secure.v_active_branch_staff_roles IS "
        "'Tenant-safe security-invoker view of canonical branch staff roles.'",
    )


def _restore_functions(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT object_name, owner_name, definition, acl_text
            FROM app_private.migration_0029_contract_state
            WHERE object_kind = 'function'
            ORDER BY object_name
            """
        )
    ).mappings().all()
    if {row["object_name"] for row in rows} != set(_FUNCTION_SIGNATURES):
        raise RuntimeError("0029 predecessor function journal is incomplete.")

    added_security_create = _prepare_private_create(
        bind, "app_security_owner"
    )
    added_executor_create = _prepare_private_create(
        bind, "app_rls_executor"
    )
    try:
        for row in rows:
            signature = row["object_name"]
            expected_owner = _FUNCTION_OWNERS[signature]
            if row["owner_name"] != expected_owner:
                raise RuntimeError(
                    f"Journal owner drift for {signature}."
                )
            if expected_owner == "migration_owner":
                _require_migration_owner(bind)
                bind.exec_driver_sql(row["definition"])
                op.execute("""
                    REVOKE ALL ON FUNCTION
                        app_private.sync_branch_staff_role_contract_fields()
                    FROM PUBLIC;
                """)
                op.execute("""
                    GRANT EXECUTE ON FUNCTION
                        app_private.sync_branch_staff_role_contract_fields()
                    TO app_runtime, app_rls_executor;
                """)
            else:
                _run_as_role(bind, expected_owner, row["definition"])

            actual = bind.execute(
                sa.text(
                    """
                    SELECT
                        pg_catalog.pg_get_userbyid(p.proowner)::text AS owner_name,
                        p.proacl::text AS acl_text
                    FROM pg_catalog.pg_proc AS p
                    WHERE p.oid = pg_catalog.to_regprocedure(:signature)
                    """
                ),
                {"signature": signature},
            ).mappings().one()
            if actual["owner_name"] != expected_owner:
                raise RuntimeError(
                    f"Function owner restoration failed for {signature}."
                )
            if actual["acl_text"] != row["acl_text"]:
                raise RuntimeError(
                    f"Function ACL drift after restoration for {signature}."
                )
    finally:
        _release_private_create(
            bind, "app_rls_executor", added_executor_create
        )
        _release_private_create(
            bind, "app_security_owner", added_security_create
        )


def _restore_legacy_objects(bind):
    for name in _LEGACY_CONSTRAINTS:
        definition = bind.execute(
            sa.text(
                """
                SELECT definition
                FROM app_private.migration_0029_contract_state
                WHERE object_kind = 'constraint'
                  AND object_name = :name
                """
            ),
            {"name": name},
        ).scalar_one()
        bind.exec_driver_sql(
            f"ALTER TABLE public.branch_staff_roles "
            f"ADD CONSTRAINT {name} {definition}"
        )

    for name in _LEGACY_INDEXES:
        definition = bind.execute(
            sa.text(
                """
                SELECT definition
                FROM app_private.migration_0029_contract_state
                WHERE object_kind = 'index'
                  AND object_name = :name
                """
            ),
            {"name": name},
        ).scalar_one()
        bind.exec_driver_sql(definition)


def _restore_expand_constraint_validation(bind):
    for name in _EXPAND_CONSTRAINTS:
        row = bind.execute(
            sa.text(
                """
                SELECT definition, metadata_json
                FROM app_private.migration_0029_contract_state
                WHERE object_kind = 'constraint'
                  AND object_name = :name
                """
            ),
            {"name": name},
        ).mappings().one()
        was_validated = bool(row["metadata_json"]["validated"])
        if was_validated:
            continue
        op.execute(
            "ALTER TABLE public.branch_staff_roles "
            f"DROP CONSTRAINT {name};"
        )
        definition = row["definition"]
        suffix = "" if "NOT VALID" in definition.upper() else " NOT VALID"
        bind.exec_driver_sql(
            "ALTER TABLE public.branch_staff_roles "
            f"ADD CONSTRAINT {name} {definition}{suffix}"
        )
        restored = bind.execute(
            sa.text(
                """
                SELECT con.convalidated
                FROM pg_catalog.pg_constraint AS con
                WHERE con.conrelid = 'public.branch_staff_roles'::regclass
                  AND con.conname = :name
                """
            ),
            {"name": name},
        ).scalar_one()
        if restored is not False:
            raise RuntimeError(
                f"Failed to restore NOT VALID state for {name}."
            )


def _restore_predecessor_view(bind):
    row = bind.execute(
        sa.text(
            """
            SELECT owner_name, definition, metadata_json
            FROM app_private.migration_0029_contract_state
            WHERE object_kind = 'view'
              AND object_name = 'app_secure.v_active_branch_staff_roles'
            """
        )
    ).mappings().one()
    if row["owner_name"] != "app_security_owner":
        raise RuntimeError("Predecessor view journal owner drift.")
    options = set(row["metadata_json"].get("reloptions", []))
    if options != {"security_barrier=true", "security_invoker=true"}:
        raise RuntimeError("Predecessor view journal options drift.")

    _run_as_role(
        bind,
        "app_security_owner",
        "CREATE VIEW app_secure.v_active_branch_staff_roles "
        "WITH (security_barrier=true, security_invoker=true) AS "
        + row["definition"],
    )
    _run_as_role(
        bind,
        "app_security_owner",
        "REVOKE ALL ON app_secure.v_active_branch_staff_roles FROM PUBLIC",
    )
    _run_as_role(
        bind,
        "app_security_owner",
        "GRANT SELECT ON app_secure.v_active_branch_staff_roles "
        "TO app_runtime, readonly_analytics",
    )
    comment = row["metadata_json"].get("comment")
    if comment:
        escaped = comment.replace("'", "''")
        _run_as_role(
            bind,
            "app_security_owner",
            "COMMENT ON VIEW app_secure.v_active_branch_staff_roles IS "
            f"'{escaped}'",
        )


def _revoke_revision_added_grants(bind):
    rows = bind.execute(
        sa.text(
            """
            SELECT object_kind, object_identity, grantee, privilege_type
            FROM app_private.migration_0029_added_grants
            ORDER BY object_kind, object_identity, grantee, privilege_type
            """
        )
    ).mappings().all()

    for row in reversed(rows):
        key = (
            row["object_kind"],
            row["object_identity"],
            row["grantee"],
            row["privilege_type"],
        )
        if key not in _ADDED_GRANT_ALLOWLIST:
            raise RuntimeError(f"Unapproved grant journal row: {key!r}.")
        if row["object_kind"] == "table":
            bind.exec_driver_sql(
                f"REVOKE {row['privilege_type']} ON TABLE "
                f"{row['object_identity']} FROM {row['grantee']}"
            )
        elif row["object_kind"] == "sequence":
            bind.exec_driver_sql(
                f"REVOKE {row['privilege_type']} ON SEQUENCE "
                f"{row['object_identity']} FROM {row['grantee']}"
            )
        elif row["object_kind"] == "schema":
            bind.exec_driver_sql(
                f"REVOKE {row['privilege_type']} ON SCHEMA "
                f"{row['object_identity']} FROM {row['grantee']}"
            )
        else:
            raise RuntimeError(
                "Unsupported journaled privilege object kind "
                f"{row['object_kind']!r}."
            )


def _check_policy_collisions(bind):
    for policy_name in _INTERNAL_POLICIES:
        count = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM pg_catalog.pg_policy
                WHERE polrelid = 'public.branch_staff_roles'::regclass
                  AND polname = :policy_name
                """
            ),
            {"policy_name": policy_name},
        ).scalar_one()
        if count:
            raise RuntimeError(f"0029 policy collision: {policy_name}.")


def upgrade() -> None:
    bind = _bind()
    _require_migration_owner(bind)
    _require_forced_owner_tables(bind)
    _require_trigger_states(bind, "O")
    _require_role_foundation(bind)
    _check_policy_collisions(bind)

    op.execute("LOCK TABLE public.organization_users IN SHARE MODE;")
    op.execute(
        "LOCK TABLE public.organization_members "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "LOCK TABLE public.branch_staff_roles "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "LOCK TABLE public.staff_roles, public.scope_types IN SHARE MODE;"
    )

    _create_state_tables(bind)
    _capture_predecessor_state(bind)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.organization_members "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DISABLE TRIGGER trg_bsr_validate_rls_context;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DISABLE TRIGGER trg_invalidate_perm_snapshot;"
    )
    _require_trigger_states(bind, "D")

    op.execute("""
        DO $rb1m2u_contract_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS om
                  ON om.id = bsr.organization_member_id
                 AND om.org_id = bsr.org_id
                LEFT JOIN public.staff_roles AS sr
                  ON sr.id = bsr.role_id
                WHERE bsr.organization_member_id IS NULL
                   OR bsr.role_id IS NULL
                   OR om.id IS NULL
                   OR om.user_id IS DISTINCT FROM bsr.user_id
                   OR sr.id IS NULL
                   OR sr.code IS DISTINCT FROM bsr.role::text
            ) THEN
                RAISE EXCEPTION
                    '0029 contract preflight: expand representations are incomplete';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS member_match
                  ON member_match.id = bsr.assigned_by
                 AND member_match.org_id = bsr.org_id
                LEFT JOIN public.organization_members AS user_match
                  ON user_match.user_id = bsr.assigned_by
                 AND user_match.org_id = bsr.org_id
                WHERE bsr.assigned_by IS NOT NULL
                  AND member_match.id IS NULL
                  AND user_match.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '0029 assigned_by identity cannot be resolved';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                JOIN public.organization_members AS member_match
                  ON member_match.id = bsr.assigned_by
                 AND member_match.org_id = bsr.org_id
                JOIN public.organization_members AS user_match
                  ON user_match.user_id = bsr.assigned_by
                 AND user_match.org_id = bsr.org_id
                WHERE bsr.assigned_by IS NOT NULL
                  AND member_match.id IS DISTINCT FROM user_match.id
            ) THEN
                RAISE EXCEPTION
                    '0029 assigned_by identity is ambiguous';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS member_match
                  ON member_match.id = bsr.revoked_by
                 AND member_match.org_id = bsr.org_id
                LEFT JOIN public.organization_members AS user_match
                  ON user_match.user_id = bsr.revoked_by
                 AND user_match.org_id = bsr.org_id
                WHERE bsr.revoked_by IS NOT NULL
                  AND member_match.id IS NULL
                  AND user_match.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '0029 revoked_by identity cannot be resolved';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                JOIN public.organization_members AS member_match
                  ON member_match.id = bsr.revoked_by
                 AND member_match.org_id = bsr.org_id
                JOIN public.organization_members AS user_match
                  ON user_match.user_id = bsr.revoked_by
                 AND user_match.org_id = bsr.org_id
                WHERE bsr.revoked_by IS NOT NULL
                  AND member_match.id IS DISTINCT FROM user_match.id
            ) THEN
                RAISE EXCEPTION
                    '0029 revoked_by identity is ambiguous';
            END IF;
        END
        $rb1m2u_contract_preflight$;
    """)

    # The actor columns still reference organization_users at predecessor 0028.
    # Drop those FKs before rewriting their representation.
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT fk_branch_staff_assigned_by;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT fk_branch_staff_revoked_by;"
    )

    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET assigned_by = om.id
        FROM public.organization_members AS om
        WHERE bsr.assigned_by IS NOT NULL
          AND om.org_id = bsr.org_id
          AND om.user_id = bsr.assigned_by
          AND NOT EXISTS (
              SELECT 1
              FROM public.organization_members AS canonical
              WHERE canonical.id = bsr.assigned_by
                AND canonical.org_id = bsr.org_id
          );
    """)
    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET revoked_by = om.id
        FROM public.organization_members AS om
        WHERE bsr.revoked_by IS NOT NULL
          AND om.org_id = bsr.org_id
          AND om.user_id = bsr.revoked_by
          AND NOT EXISTS (
              SELECT 1
              FROM public.organization_members AS canonical
              WHERE canonical.id = bsr.revoked_by
                AND canonical.org_id = bsr.org_id
          );
    """)

    op.execute("""
        DO $rb1m2u_actor_verify$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS om
                  ON om.id = bsr.assigned_by
                 AND om.org_id = bsr.org_id
                WHERE bsr.assigned_by IS NOT NULL
                  AND om.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '0029 assigned_by conversion verification failed';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                LEFT JOIN public.organization_members AS om
                  ON om.id = bsr.revoked_by
                 AND om.org_id = bsr.org_id
                WHERE bsr.revoked_by IS NOT NULL
                  AND om.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '0029 revoked_by conversion verification failed';
            END IF;
        END
        $rb1m2u_actor_verify$;
    """)

    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_assigned_by
        FOREIGN KEY (assigned_by, org_id)
        REFERENCES public.organization_members(id, org_id)
        ON DELETE RESTRICT
        NOT VALID;
    """)
    op.execute("""
        ALTER TABLE public.branch_staff_roles
        ADD CONSTRAINT fk_bsr_revoked_by
        FOREIGN KEY (revoked_by, org_id)
        REFERENCES public.organization_members(id, org_id)
        ON DELETE RESTRICT
        NOT VALID;
    """)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_member_id;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_member_org;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_role_id;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_scope_type_id;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_assigned_by;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "VALIDATE CONSTRAINT fk_bsr_revoked_by;"
    )

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN organization_member_id SET NOT NULL;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN role_id SET NOT NULL;"
    )

    op.execute(
        "ALTER TABLE public.organization_members "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ENABLE TRIGGER trg_invalidate_perm_snapshot;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ENABLE TRIGGER trg_bsr_validate_rls_context;"
    )
    _require_forced_owner_tables(bind)
    _require_trigger_states(bind, "O")

    _prepare_runtime_authority(bind)
    _replace_runtime_functions(bind)

    op.execute(
        "DROP TRIGGER trg_sync_branch_staff_role_contract_fields "
        "ON public.branch_staff_roles;"
    )
    op.execute(
        "DROP FUNCTION app_private.sync_branch_staff_role_contract_fields() "
        "RESTRICT;"
    )

    _drop_secure_view(bind)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT fk_branch_staff_user_org;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT exclude_overlapping_staff_assignments;"
    )
    op.execute("DROP INDEX public.ix_branch_staff_user_active;")
    op.execute("DROP INDEX public.ix_branch_staff_branch_active;")
    op.execute(
        "ALTER TABLE public.branch_staff_roles DROP COLUMN user_id;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles DROP COLUMN role;"
    )

    _create_post_contract_view(bind)
    _require_app_runtime_compile_authority(bind)
    _require_forced_owner_tables(bind)
    _require_trigger_states(bind, "O")
    _reject_public_private_create(bind)


def downgrade() -> None:
    bind = _bind()
    _require_migration_owner(bind)
    _require_forced_owner_tables(bind)
    _require_forced_owner_organization_users(bind)
    _require_trigger_states(bind, "O")
    _require_audit_trigger_state(bind, "O")
    _require_role_foundation(bind)
    _require_app_runtime_compile_authority(bind)

    for marker in (_STATE_TABLE, _GRANT_TABLE):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:name)"),
            {"name": marker},
        ).scalar_one() is None:
            raise RuntimeError(f"0029 downgrade marker absent: {marker}.")

    op.execute(
        "LOCK TABLE public.organization_users "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "LOCK TABLE public.organization_members "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute(
        "LOCK TABLE public.branch_staff_roles "
        "IN SHARE ROW EXCLUSIVE MODE;"
    )
    op.execute("LOCK TABLE public.staff_roles IN SHARE MODE;")

    op.execute(
        "ALTER TABLE public.organization_users "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.organization_members "
        "NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DISABLE TRIGGER trg_bsr_validate_rls_context;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DISABLE TRIGGER trg_invalidate_perm_snapshot;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DISABLE TRIGGER trg_audit_branch_staff_roles;"
    )
    _require_trigger_states(bind, "D")
    _require_audit_trigger_state(bind, "D")

    # Refuse before any data rewrite if canonical roles cannot be represented
    # by the predecessor four-value enum.
    op.execute("""
        DO $rb1m2u_downgrade_role_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles AS bsr
                JOIN public.staff_roles AS sr
                  ON sr.id = bsr.role_id
                WHERE sr.code NOT IN (
                    'manager',
                    'trainer',
                    'receptionist',
                    'auditor'
                )
            ) THEN
                RAISE EXCEPTION
                    '0029 downgrade cannot represent canonical owner/admin '
                    'roles in branch_staff_role_enum';
            END IF;
        END
        $rb1m2u_downgrade_role_preflight$;
    """)

    _drop_secure_view(bind)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ADD COLUMN user_id UUID NULL;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ADD COLUMN role public.branch_staff_role_enum NULL;"
    )

    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET user_id = om.user_id
        FROM public.organization_members AS om
        WHERE om.id = bsr.organization_member_id
          AND om.org_id = bsr.org_id;
    """)
    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET role = sr.code::public.branch_staff_role_enum
        FROM public.staff_roles AS sr
        WHERE sr.id = bsr.role_id;
    """)

    op.execute("""
        DO $rb1m2u_downgrade_identity_verify$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.branch_staff_roles
                WHERE user_id IS NULL OR role IS NULL
            ) THEN
                RAISE EXCEPTION
                    '0029 downgrade failed to reconstruct legacy user/role';
            END IF;
        END
        $rb1m2u_downgrade_identity_verify$;
    """)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN user_id SET NOT NULL;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN role SET NOT NULL;"
    )

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT fk_bsr_assigned_by;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "DROP CONSTRAINT fk_bsr_revoked_by;"
    )

    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET assigned_by = om.user_id
        FROM public.organization_members AS om
        WHERE bsr.assigned_by = om.id
          AND bsr.org_id = om.org_id;
    """)
    op.execute("""
        UPDATE public.branch_staff_roles AS bsr
        SET revoked_by = om.user_id
        FROM public.organization_members AS om
        WHERE bsr.revoked_by = om.id
          AND bsr.org_id = om.org_id;
    """)

    _restore_legacy_objects(bind)
    op.execute(
        "ALTER TABLE public.organization_users "
        "FORCE ROW LEVEL SECURITY;"
    )
    _require_forced_owner_organization_users(bind)
    _restore_expand_constraint_validation(bind)

    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN organization_member_id DROP NOT NULL;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ALTER COLUMN role_id DROP NOT NULL;"
    )

    _restore_functions(bind)
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ENABLE TRIGGER trg_audit_branch_staff_roles;"
    )
    _require_audit_trigger_state(bind, "O")
    op.execute("""
        CREATE TRIGGER trg_sync_branch_staff_role_contract_fields
        BEFORE INSERT OR UPDATE
        ON public.branch_staff_roles
        FOR EACH ROW
        EXECUTE FUNCTION
            app_private.sync_branch_staff_role_contract_fields();
    """)

    for policy_name in reversed(_INTERNAL_POLICIES):
        op.execute(
            "DROP POLICY "
            f"{policy_name} ON public.branch_staff_roles;"
        )

    _restore_predecessor_view(bind)

    op.execute(
        "ALTER TABLE public.organization_members "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ENABLE TRIGGER trg_invalidate_perm_snapshot;"
    )
    op.execute(
        "ALTER TABLE public.branch_staff_roles "
        "ENABLE TRIGGER trg_bsr_validate_rls_context;"
    )

    _revoke_revision_added_grants(bind)

    op.execute(
        "DROP TABLE app_private.migration_0029_added_grants RESTRICT;"
    )
    op.execute(
        "DROP TABLE app_private.migration_0029_contract_state RESTRICT;"
    )

    _require_forced_owner_tables(bind)
    _require_forced_owner_organization_users(bind)
    _require_trigger_states(bind, "O")
    _require_audit_trigger_state(bind, "O")
    _reject_public_private_create(bind)
