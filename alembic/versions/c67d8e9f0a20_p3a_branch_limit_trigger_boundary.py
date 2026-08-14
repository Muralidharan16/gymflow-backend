"""P3A: harden the branch-limit trigger under the reduced auth boundary.

Revision ID: c67d8e9f0a20
Revises: c57d8e9f0a1f
Create Date: 2026-08-14

The legacy ``enforce_max_branches()`` trigger used
``SELECT ... FROM organizations ... FOR UPDATE`` as the ordinary INSERT caller.
P3A correctly removes direct organization UPDATE authority from ``auth_runtime``;
PostgreSQL locking SELECTs require UPDATE authority, so verified first-branch
onboarding failed even though the caller retained its certified organization
SELECT/INSERT bootstrap contract.

Do not restore broad UPDATE. Instead, this revision moves only the invariant
check behind the existing NOLOGIN/NOBYPASSRLS ``app_security_owner`` identity,
grants that identity SELECT on the single protected ``max_branches`` column,
and replaces the organization-row lock with the same organization-scoped
transaction advisory lock already used by the hardened critical-branch guard.
A companion BEFORE UPDATE trigger on ``organizations.max_branches`` acquires
that same advisory lock, preserving serialization between branch creation and
limit changes without granting any runtime identity protected-field mutation.

The hardened trigger is tenant-bound, runs with ``row_security=on``, uses only
schema-qualified relations under ``search_path=pg_catalog``, and is not directly
executable by PUBLIC/API/auth roles. Downgrade restores the exact legacy
SECURITY INVOKER/FOR UPDATE behavior and removes every revision-owned grant and
serializer object.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c67d8e9f0a20"
down_revision = "c57d8e9f0a1f"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_AUTH_ROLE = "auth_runtime"
_API_ROLE = "app_runtime"
_MAX_SIGNATURE = "public.enforce_max_branches()"
_SERIALIZER_SIGNATURE = "public.serialize_max_branches_update()"
_BRANCH_LOCK_SEED = 81924356


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_name,
                current_user::text AS current_name,
                role_data.rolsuper,
                role_data.rolinherit,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = current_user
            """
        )
    ).mappings().one()
    if (
        row["session_name"] != _MIGRATION_OWNER
        or row["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("c67 requires session_user=current_user=migration_owner")
    if any(
        bool(row[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced migration contract")

    security_owner = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = :role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).mappings().one_or_none()
    if security_owner is None:
        raise RuntimeError("app_security_owner is missing")
    if any(bool(security_owner[key]) for key in security_owner):
        raise RuntimeError(
            "app_security_owner violates NOLOGIN/NOINHERIT/NOBYPASSRLS"
        )

    if not bool(
        _scalar(
            bind,
            "SELECT pg_catalog.pg_has_role(session_user, CAST(:role AS name), 'SET')",
            {"role": _SECURITY_OWNER},
        )
    ):
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner")

    if not bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(CAST(:role AS name), 'public', 'USAGE')",
            {"role": _SECURITY_OWNER},
        )
    ):
        raise RuntimeError("app_security_owner lacks required public schema USAGE")
    if bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(CAST(:role AS name), 'public', 'CREATE')",
            {"role": _SECURITY_OWNER},
        )
    ):
        raise RuntimeError(
            "c67 refuses to adopt pre-existing public CREATE for app_security_owner"
        )


def _function_row(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                procedure_data.proconfig,
                procedure_data.proacl IS NULL AS acl_is_null,
                pg_catalog.pg_get_functiondef(procedure_data.oid)::text AS definition,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            procedure_data.proacl,
                            pg_catalog.acldefault('f', procedure_data.proowner)
                        )
                    ) AS acl_data
                    WHERE acl_data.grantee = 0
                      AND acl_data.privilege_type = 'EXECUTE'
                ) AS public_execute
            FROM pg_catalog.pg_proc AS procedure_data
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            WHERE procedure_data.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().one()


def _trigger_definition(bind, *, table_name: str, trigger_name: str) -> str | None:
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_triggerdef(trigger_data.oid, true)::text
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = :table_name
              AND trigger_data.tgname = :trigger_name
              AND NOT trigger_data.tgisinternal
            """
        ),
        {"table_name": table_name, "trigger_name": trigger_name},
    ).scalar_one_or_none()


def _direct_relation_privileges(bind, *, role_name: str, table_name: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation_data.relacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :table_name
                  AND grantee_role.rolname = :role_name
                """
            ),
            {"role_name": role_name, "table_name": table_name},
        ).scalars().all()
    )


def _direct_column_acl(
    bind, *, role_name: str, table_name: str, column_name: str
) -> set[tuple[str, bool, str]]:
    return {
        (str(row[0]), bool(row[1]), str(row[2]))
        for row in bind.execute(
            sa.text(
                """
                SELECT
                    acl_data.privilege_type::text,
                    acl_data.is_grantable,
                    grantor_role.rolname::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute_data
                  ON attribute_data.attrelid = relation_data.oid
                 AND attribute_data.attnum > 0
                 AND NOT attribute_data.attisdropped
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee_role
                  ON grantee_role.oid = acl_data.grantee
                JOIN pg_catalog.pg_roles AS grantor_role
                  ON grantor_role.oid = acl_data.grantor
                WHERE namespace_data.nspname = 'public'
                  AND relation_data.relname = :table_name
                  AND attribute_data.attname = :column_name
                  AND grantee_role.rolname = :role_name
                """
            ),
            {
                "role_name": role_name,
                "table_name": table_name,
                "column_name": column_name,
            },
        ).all()
    }


def _has_direct_column_select(
    bind, *, role_name: str, table_name: str, column_name: str
) -> bool:
    return any(
        privilege == "SELECT"
        for privilege, _grantable, _grantor in _direct_column_acl(
            bind,
            role_name=role_name,
            table_name=table_name,
            column_name=column_name,
        )
    )


def _normalized(definition: str | None) -> str:
    return " ".join((definition or "").upper().split())


def _require_predecessor(bind) -> None:
    _require_identity(bind)

    max_function = _function_row(bind, _MAX_SIGNATURE)
    if max_function["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError("legacy enforce_max_branches owner drifted")
    if bool(max_function["security_definer"]):
        raise RuntimeError("legacy enforce_max_branches unexpectedly SECURITY DEFINER")
    if max_function["proconfig"] is not None:
        raise RuntimeError("legacy enforce_max_branches unexpectedly has function settings")
    if not bool(max_function["public_execute"]):
        raise RuntimeError("legacy enforce_max_branches PUBLIC EXECUTE drifted")

    legacy_definition = " ".join(str(max_function["definition"]).split()).lower()
    for token in (
        "from organizations",
        "for update",
        "from org_branches",
        "count(*)",
        "new.org_id",
    ):
        if token not in legacy_definition:
            raise RuntimeError(
                f"legacy enforce_max_branches definition drifted: missing {token!r}"
            )

    trigger = _normalized(
        _trigger_definition(
            bind,
            table_name="org_branches",
            trigger_name="trg_enforce_max_branches",
        )
    )
    if (
        "BEFORE INSERT ON" not in trigger
        or "ENFORCE_MAX_BRANCHES()" not in trigger
        or "FOR EACH ROW" not in trigger
    ):
        raise RuntimeError("legacy trg_enforce_max_branches definition drifted")

    if _scalar(bind, "SELECT pg_catalog.to_regprocedure(:sig)", {"sig": _SERIALIZER_SIGNATURE}) is not None:
        raise RuntimeError("c67 serializer function already exists")
    if _trigger_definition(
        bind,
        table_name="organizations",
        trigger_name="trg_serialize_max_branches_update",
    ) is not None:
        raise RuntimeError("c67 serializer trigger already exists")

    if _direct_relation_privileges(
        bind, role_name=_SECURITY_OWNER, table_name="organizations"
    ):
        raise RuntimeError(
            "app_security_owner unexpectedly has organizations relation ACL"
        )
    if _has_direct_column_select(
        bind,
        role_name=_SECURITY_OWNER,
        table_name="organizations",
        column_name="max_branches",
    ):
        raise RuntimeError(
            "c57 predecessor already grants app_security_owner max_branches SELECT"
        )
    if not _has_direct_column_select(
        bind,
        role_name=_SECURITY_OWNER,
        table_name="organizations",
        column_name="id",
    ):
        raise RuntimeError("app_security_owner lost organizations.id SELECT")
    for column_name in ("id", "org_id"):
        if not _has_direct_column_select(
            bind,
            role_name=_SECURITY_OWNER,
            table_name="org_branches",
            column_name=column_name,
        ):
            raise RuntimeError(
                f"app_security_owner lost org_branches.{column_name} SELECT"
            )
    if "SELECT" in _direct_relation_privileges(
        bind, role_name=_SECURITY_OWNER, table_name="org_branches"
    ):
        raise RuntimeError("app_security_owner has broad org_branches SELECT")

    if _direct_relation_privileges(
        bind, role_name=_AUTH_ROLE, table_name="organizations"
    ) != {"INSERT", "SELECT"}:
        raise RuntimeError("auth_runtime organizations relation ACL drifted")
    if "UPDATE" in _direct_relation_privileges(
        bind, role_name=_AUTH_ROLE, table_name="organizations"
    ):
        raise RuntimeError("auth_runtime unexpectedly regained organizations UPDATE")
    if _direct_relation_privileges(
        bind, role_name=_API_ROLE, table_name="organizations"
    ):
        raise RuntimeError("app_runtime unexpectedly has organizations relation ACL")

    critical_guard = _function_row(bind, "public.prevent_critical_branch_deletion()")
    critical_source = " ".join(str(critical_guard["definition"]).split()).lower()
    if (
        "pg_advisory_xact_lock" not in critical_source
        or str(_BRANCH_LOCK_SEED) not in critical_source
    ):
        raise RuntimeError(
            "hardened critical-branch guard no longer uses the shared invariant lock"
        )


def _create_forward_objects() -> None:
    op.execute("DROP TRIGGER trg_enforce_max_branches ON public.org_branches")
    op.execute("DROP FUNCTION public.enforce_max_branches()")

    op.execute(
        f"""
        CREATE FUNCTION public.enforce_max_branches()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_org_text text;
            v_org_id uuid;
            current_count integer;
            max_allowed integer;
        BEGIN
            v_org_text := pg_catalog.current_setting('app.current_org_id', true);
            IF v_org_text IS NULL OR pg_catalog.btrim(v_org_text) = '' THEN
                RAISE EXCEPTION 'branch-limit tenant context is required'
                    USING ERRCODE = '42501';
            END IF;

            BEGIN
                v_org_id := v_org_text::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'branch-limit tenant context is invalid'
                        USING ERRCODE = '42501';
            END;

            IF NEW.org_id IS NULL OR NEW.org_id IS DISTINCT FROM v_org_id THEN
                RAISE EXCEPTION 'branch-limit tenant context mismatch'
                    USING ERRCODE = '42501';
            END IF;

            -- Coordinate branch inserts, critical branch deletions, and
            -- max_branches changes without requiring tenant-root UPDATE.
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    NEW.org_id::text,
                    {_BRANCH_LOCK_SEED}::bigint
                )
            );

            SELECT organization.max_branches
            INTO max_allowed
            FROM public.organizations AS organization
            WHERE organization.id = NEW.org_id;

            IF max_allowed IS NULL THEN
                RAISE EXCEPTION 'organization % is unavailable for branch creation', NEW.org_id
                    USING ERRCODE = '23503';
            END IF;

            SELECT pg_catalog.count(branch.id)
            INTO current_count
            FROM public.org_branches AS branch
            WHERE branch.org_id = NEW.org_id;

            IF current_count >= max_allowed THEN
                RAISE EXCEPTION
                    'Organization has reached its maximum branch limit (%)',
                    max_allowed;
            END IF;

            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.enforce_max_branches() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER trg_enforce_max_branches
        BEFORE INSERT ON public.org_branches
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_max_branches()
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION public.serialize_max_branches_update()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    OLD.id::text,
                    {_BRANCH_LOCK_SEED}::bigint
                )
            );
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.serialize_max_branches_update() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER trg_serialize_max_branches_update
        BEFORE UPDATE OF max_branches ON public.organizations
        FOR EACH ROW
        WHEN (OLD.max_branches IS DISTINCT FROM NEW.max_branches)
        EXECUTE FUNCTION public.serialize_max_branches_update()
        """
    )

    # ALTER OWNER requires temporary CREATE for the target role. Restore the
    # schema ACL immediately; app_security_owner must never keep schema CREATE.
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.enforce_max_branches() OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")


def _require_forward(bind) -> None:
    _require_identity(bind)

    if bool(
        _scalar(
            bind,
            "SELECT pg_catalog.has_schema_privilege(CAST(:role AS name), 'public', 'CREATE')",
            {"role": _SECURITY_OWNER},
        )
    ):
        raise RuntimeError("app_security_owner retained public schema CREATE")

    max_acl = _direct_column_acl(
        bind,
        role_name=_SECURITY_OWNER,
        table_name="organizations",
        column_name="max_branches",
    )
    if max_acl != {("SELECT", False, _MIGRATION_OWNER)}:
        raise RuntimeError(
            f"app_security_owner max_branches ACL drifted: {sorted(max_acl)!r}"
        )
    if _direct_relation_privileges(
        bind, role_name=_SECURITY_OWNER, table_name="organizations"
    ):
        raise RuntimeError(
            "app_security_owner gained organizations relation-level privilege"
        )

    max_function = _function_row(bind, _MAX_SIGNATURE)
    if max_function["owner_name"] != _SECURITY_OWNER:
        raise RuntimeError("enforce_max_branches owner is not app_security_owner")
    if not bool(max_function["security_definer"]):
        raise RuntimeError("enforce_max_branches is not SECURITY DEFINER")
    if set(max_function["proconfig"] or []) != {
        "search_path=pg_catalog",
        "row_security=on",
    }:
        raise RuntimeError("enforce_max_branches function settings drifted")
    if bool(max_function["public_execute"]):
        raise RuntimeError("PUBLIC can execute enforce_max_branches")

    max_definition = " ".join(str(max_function["definition"]).split()).lower()
    for token in (
        "app.current_org_id",
        "pg_advisory_xact_lock",
        "hashtextextended",
        str(_BRANCH_LOCK_SEED),
        "public.organizations",
        "organization.max_branches",
        "public.org_branches",
        "count(branch.id)",
    ):
        if token not in max_definition:
            raise RuntimeError(
                f"hardened enforce_max_branches definition drifted: missing {token!r}"
            )
    if "for update" in max_definition:
        raise RuntimeError("hardened enforce_max_branches retained FOR UPDATE")

    serializer = _function_row(bind, _SERIALIZER_SIGNATURE)
    if serializer["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError("max_branches serializer owner drifted")
    if bool(serializer["security_definer"]):
        raise RuntimeError("max_branches serializer must remain SECURITY INVOKER")
    if set(serializer["proconfig"] or []) != {"search_path=pg_catalog"}:
        raise RuntimeError("max_branches serializer settings drifted")
    if bool(serializer["public_execute"]):
        raise RuntimeError("PUBLIC can execute max_branches serializer")
    serializer_definition = " ".join(str(serializer["definition"]).split()).lower()
    for token in (
        "pg_advisory_xact_lock",
        "hashtextextended",
        str(_BRANCH_LOCK_SEED),
        "old.id",
    ):
        if token not in serializer_definition:
            raise RuntimeError(
                f"max_branches serializer definition drifted: missing {token!r}"
            )

    insert_trigger = _normalized(
        _trigger_definition(
            bind,
            table_name="org_branches",
            trigger_name="trg_enforce_max_branches",
        )
    )
    if (
        "BEFORE INSERT ON" not in insert_trigger
        or "ENFORCE_MAX_BRANCHES()" not in insert_trigger
        or "FOR EACH ROW" not in insert_trigger
    ):
        raise RuntimeError("hardened trg_enforce_max_branches drifted")

    update_trigger = _normalized(
        _trigger_definition(
            bind,
            table_name="organizations",
            trigger_name="trg_serialize_max_branches_update",
        )
    )
    if (
        "BEFORE UPDATE OF MAX_BRANCHES ON" not in update_trigger
        or "SERIALIZE_MAX_BRANCHES_UPDATE()" not in update_trigger
        or "OLD.MAX_BRANCHES IS DISTINCT FROM NEW.MAX_BRANCHES" not in update_trigger
    ):
        raise RuntimeError("max_branches serialization trigger drifted")

    if _direct_relation_privileges(
        bind, role_name=_AUTH_ROLE, table_name="organizations"
    ) != {"INSERT", "SELECT"}:
        raise RuntimeError("c67 widened or narrowed auth organizations relation ACL")
    if _direct_relation_privileges(
        bind, role_name=_API_ROLE, table_name="organizations"
    ):
        raise RuntimeError("c67 leaked organizations relation ACL to app_runtime")


def _drop_security_owner_max_function(bind) -> None:
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    current = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    if current != (_MIGRATION_OWNER, _SECURITY_OWNER):
        raise RuntimeError("failed to enter app_security_owner for c67 downgrade")
    bind.execute(sa.text("DROP FUNCTION public.enforce_max_branches()"))
    bind.execute(sa.text("RESET ROLE"))
    current = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    if current != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("failed to restore migration_owner after c67 downgrade")


def _restore_predecessor() -> None:
    op.execute(
        """
        CREATE FUNCTION public.enforce_max_branches()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        AS $function$
        DECLARE
            current_count INTEGER;
            max_allowed INTEGER;
        BEGIN
            SELECT max_branches
            INTO max_allowed
            FROM organizations
            WHERE id = NEW.org_id
            FOR UPDATE;

            SELECT COUNT(*)
            INTO current_count
            FROM org_branches
            WHERE org_id = NEW.org_id;

            IF current_count >= max_allowed THEN
                RAISE EXCEPTION
                    'Organization has reached its maximum branch limit (%)',
                    max_allowed;
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute("ALTER FUNCTION public.enforce_max_branches() RESET ALL")
    op.execute("GRANT EXECUTE ON FUNCTION public.enforce_max_branches() TO PUBLIC")
    op.execute(
        """
        CREATE TRIGGER trg_enforce_max_branches
        BEFORE INSERT ON public.org_branches
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_max_branches()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)

    op.execute(
        "GRANT SELECT (max_branches) ON TABLE public.organizations TO app_security_owner"
    )
    _create_forward_objects()
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_forward(bind)

    op.execute(
        "DROP TRIGGER trg_serialize_max_branches_update ON public.organizations"
    )
    op.execute("DROP FUNCTION public.serialize_max_branches_update()")
    op.execute("DROP TRIGGER trg_enforce_max_branches ON public.org_branches")
    _drop_security_owner_max_function(bind)

    _restore_predecessor()
    op.execute(
        "REVOKE SELECT (max_branches) ON TABLE public.organizations FROM app_security_owner"
    )
    _require_predecessor(bind)
