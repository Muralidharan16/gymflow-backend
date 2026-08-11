"""Harden legacy branch triggers for the reduced runtime boundary.

Revision ID: 8192a3b4c5d6
Revises: 708192a3b4c5
Create Date: 2026-08-11

The legacy branch-state triggers predate the lifecycle control plane. Two of
those triggers ran on every UPDATE of ``org_branch_state``:

* ``enforce_branch_rbac()`` unconditionally queried ``gym_owners``;
* ``prevent_critical_branch_deletion()`` unconditionally locked
  ``organizations`` with ``FOR UPDATE``.

Lifecycle-only updates therefore acquired unrelated legacy dependencies even
when neither ``branch_status`` nor ``deleted_at`` changed. Granting ordinary
runtime broad access to either table would defeat the least-privilege boundary
established by 708192a3b4c5.

This revision scopes the legacy triggers to the columns they actually protect,
keeps the owner-role lookup behind a tightly-owned SECURITY DEFINER function,
and replaces the tenant-root row lock with an organization-scoped advisory
transaction lock. ``app_runtime`` remains unable to read ``gym_owners``.

Downgrade restores the exact 0007/0005 predecessor trigger behaviour and
removes the revision-owned column privileges.
"""

from alembic import op
import sqlalchemy as sa


revision = "8192a3b4c5d6"
down_revision = "708192a3b4c5"
branch_labels = None
depends_on = None

_RBAC_SIGNATURE = "public.enforce_branch_rbac()"
_DELETE_SIGNATURE = "public.prevent_critical_branch_deletion()"
_SECURITY_OWNER = "app_security_owner"
_ADVISORY_LOCK_SEED = 81924356


def _require_migration_owner(bind) -> None:
    session_user, current_user = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    if session_user != "migration_owner" or current_user != "migration_owner":
        raise RuntimeError(
            "8192a3b4c5d6 requires session_user=current_user=migration_owner; "
            f"observed session_user={session_user!r}, current_user={current_user!r}."
        )


def _function_contract(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid IS NOT NULL AS function_exists,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                pg_catalog.pg_get_functiondef(procedure_data.oid) AS function_definition
            FROM (SELECT pg_catalog.to_regprocedure(:signature) AS oid) AS requested
            LEFT JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = requested.oid
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            """
        ),
        {"signature": signature},
    ).mappings().one()


def _trigger_definition(bind, trigger_name: str) -> str | None:
    """Return trigger DDL after structurally pinning schema/table/name.

    PostgreSQL's pretty-printer may omit schema qualification, so callers must
    validate semantic clauses rather than compare a rendered table spelling.
    """

    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_triggerdef(trigger_data.oid, true)
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_class AS relation_data
              ON relation_data.oid = trigger_data.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace_data
              ON namespace_data.oid = relation_data.relnamespace
            WHERE namespace_data.nspname = 'public'
              AND relation_data.relname = 'org_branch_state'
              AND trigger_data.tgname = :trigger_name
              AND NOT trigger_data.tgisinternal
            """
        ),
        {"trigger_name": trigger_name},
    ).scalar_one_or_none()


def _is_unscoped_update_trigger(definition: str | None, function_name: str) -> bool:
    if not definition:
        return False
    normalized = " ".join(definition.upper().split())
    return (
        "BEFORE UPDATE ON" in normalized
        and "BEFORE UPDATE OF" not in normalized
        and function_name.upper() in normalized
    )


def _is_scoped_update_trigger(
    definition: str | None,
    *,
    function_name: str,
    columns: tuple[str, ...],
    requires_when: bool,
) -> bool:
    if not definition:
        return False
    normalized = " ".join(definition.upper().split())
    if "BEFORE UPDATE OF" not in normalized:
        return False
    if function_name.upper() not in normalized:
        return False
    if requires_when and " WHEN " not in normalized:
        return False
    return all(column.upper() in normalized for column in columns)


def _require_predecessor(bind) -> None:
    _require_migration_owner(bind)

    role_row = bind.execute(
        sa.text(
            """
            SELECT
                role_data.rolname IS NOT NULL AS role_exists,
                role_data.rolcanlogin,
                role_data.rolsuper,
                role_data.rolbypassrls,
                pg_catalog.pg_has_role(
                    session_user,
                    CAST(:role_name AS name),
                    'SET'
                ) AS migration_owner_can_set,
                pg_catalog.has_schema_privilege(
                    CAST(:role_name AS name),
                    'public',
                    'USAGE'
                ) AS security_owner_has_public_usage,
                pg_catalog.has_schema_privilege(
                    CAST(:role_name AS name),
                    'public',
                    'CREATE'
                ) AS security_owner_has_public_create
            FROM (SELECT 1) AS singleton
            LEFT JOIN pg_catalog.pg_roles AS role_data
              ON role_data.rolname = :role_name
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).mappings().one()
    if not role_row["role_exists"]:
        raise RuntimeError("Required managed role app_security_owner is absent.")
    if role_row["rolcanlogin"] or role_row["rolsuper"] or role_row["rolbypassrls"]:
        raise RuntimeError("app_security_owner must remain NOLOGIN/NOSUPERUSER/NOBYPASSRLS.")
    if not role_row["migration_owner_can_set"]:
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner.")
    if not role_row["security_owner_has_public_usage"]:
        raise RuntimeError("app_security_owner lacks required USAGE on public schema.")
    if role_row["security_owner_has_public_create"]:
        raise RuntimeError(
            "8192 refuses to adopt pre-existing CREATE capability for app_security_owner on public."
        )

    preexisting_columns = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'gym_owners'
                  AND grantee = :role_name
                  AND privilege_type = 'SELECT'
                  AND column_name IN ('id', 'org_id', 'role')
            )
            """
        ),
        {"role_name": _SECURITY_OWNER},
    ).scalar_one()
    if preexisting_columns:
        raise RuntimeError(
            "8192 refuses to adopt pre-existing app_security_owner SELECT column grants on gym_owners."
        )

    rbac = _function_contract(bind, _RBAC_SIGNATURE)
    delete_guard = _function_contract(bind, _DELETE_SIGNATURE)
    if not rbac["function_exists"] or not delete_guard["function_exists"]:
        raise RuntimeError("8192 predecessor branch trigger functions are missing.")
    if rbac["owner_name"] != "migration_owner" or rbac["security_definer"]:
        raise RuntimeError("8192 predecessor enforce_branch_rbac ownership/security drifted.")
    if delete_guard["owner_name"] != "migration_owner" or delete_guard["security_definer"]:
        raise RuntimeError(
            "8192 predecessor prevent_critical_branch_deletion ownership/security drifted."
        )
    if "FROM gym_owners" not in rbac["function_definition"]:
        raise RuntimeError("8192 predecessor enforce_branch_rbac body drifted.")
    if (
        "FROM organizations" not in delete_guard["function_definition"]
        or "FOR UPDATE" not in delete_guard["function_definition"]
    ):
        raise RuntimeError("8192 predecessor critical-deletion guard body drifted.")

    if not _is_unscoped_update_trigger(
        _trigger_definition(bind, "trg_branch_rbac"),
        "enforce_branch_rbac",
    ):
        raise RuntimeError("8192 predecessor trg_branch_rbac definition drifted.")
    if not _is_unscoped_update_trigger(
        _trigger_definition(bind, "trg_prevent_critical_branch_deletion"),
        "prevent_critical_branch_deletion",
    ):
        raise RuntimeError(
            "8192 predecessor trg_prevent_critical_branch_deletion definition drifted."
        )


def _drop_predecessor_objects() -> None:
    op.execute("DROP TRIGGER trg_branch_rbac ON public.org_branch_state")
    op.execute("DROP FUNCTION public.enforce_branch_rbac()")
    op.execute(
        "DROP TRIGGER trg_prevent_critical_branch_deletion ON public.org_branch_state"
    )
    op.execute("DROP FUNCTION public.prevent_critical_branch_deletion()")


def _create_forward_delete_guard() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.prevent_critical_branch_deletion()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public, pg_temp
        AS $function$
        DECLARE
            active_count integer;
        BEGIN
            IF NOT (
                OLD.deleted_at IS NULL
                AND NEW.deleted_at IS NOT NULL
            ) THEN
                RETURN NEW;
            END IF;

            -- Serialize destructive branch changes per organization without
            -- requiring UPDATE capability on the tenant-root organizations row.
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    OLD.org_id::text,
                    {_ADVISORY_LOCK_SEED}::bigint
                )
            );

            SELECT count(*)
            INTO active_count
            FROM public.org_branch_state AS branch_state
            WHERE branch_state.org_id = OLD.org_id
              AND branch_state.deleted_at IS NULL;

            IF OLD.is_primary = TRUE THEN
                RAISE EXCEPTION 'Cannot delete the primary branch';
            END IF;

            IF active_count <= 1 THEN
                RAISE EXCEPTION 'Cannot delete the last branch of an organization';
            END IF;

            RETURN NEW;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prevent_critical_branch_deletion
        BEFORE UPDATE OF deleted_at ON public.org_branch_state
        FOR EACH ROW
        WHEN (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
        EXECUTE FUNCTION public.prevent_critical_branch_deletion()
        """
    )


def _create_forward_rbac_guard() -> None:
    op.execute(
        """
        GRANT SELECT (id, org_id, role)
        ON TABLE public.gym_owners
        TO app_security_owner
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_branch_rbac()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $function$
        DECLARE
            actor_role text;
            actor_id uuid;
        BEGIN
            -- Unrelated lifecycle/control-plane updates must not acquire a
            -- dependency on the legacy staff table.
            IF OLD.deleted_at IS NOT DISTINCT FROM NEW.deleted_at
               AND OLD.branch_status IS NOT DISTINCT FROM NEW.branch_status THEN
                RETURN NEW;
            END IF;

            actor_id := NULLIF(
                pg_catalog.current_setting('app.current_user_id', true),
                ''
            )::uuid;

            IF actor_id IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT owner_data.role::text
            INTO actor_role
            FROM public.gym_owners AS owner_data
            WHERE owner_data.id = actor_id
              AND owner_data.org_id = OLD.org_id;

            IF actor_role IS NULL THEN
                RAISE EXCEPTION 'Actor % has no membership in org %', actor_id, OLD.org_id;
            END IF;

            IF NEW.deleted_at IS NOT NULL
               AND OLD.deleted_at IS NULL
               AND actor_role NOT IN ('owner') THEN
                RAISE EXCEPTION 'Insufficient privileges: only owners can soft-delete branches';
            END IF;

            IF OLD.branch_status IS DISTINCT FROM NEW.branch_status
               AND actor_role NOT IN ('owner', 'admin') THEN
                RAISE EXCEPTION 'Insufficient privileges: staff cannot change branch status';
            END IF;

            IF OLD.branch_status = 'archived'
               AND NEW.branch_status = 'active'
               AND actor_role NOT IN ('owner') THEN
                RAISE EXCEPTION 'Insufficient privileges: only owners can restore archived branches';
            END IF;

            RETURN NEW;
        END;
        $function$;
        """
    )

    # ALTER OWNER requires the target owner to have CREATE on the containing
    # schema. Grant it only for the ownership transfer and restore the schema
    # boundary immediately afterward.
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.enforce_branch_rbac() OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")

    op.execute("REVOKE ALL ON FUNCTION public.enforce_branch_rbac() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.enforce_branch_rbac() TO app_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION public.enforce_branch_rbac() TO auth_runtime")

    op.execute(
        """
        CREATE TRIGGER trg_branch_rbac
        BEFORE UPDATE OF deleted_at, branch_status ON public.org_branch_state
        FOR EACH ROW
        WHEN (
            OLD.deleted_at IS DISTINCT FROM NEW.deleted_at
            OR OLD.branch_status IS DISTINCT FROM NEW.branch_status
        )
        EXECUTE FUNCTION public.enforce_branch_rbac()
        """
    )


def _verify_forward(bind) -> None:
    rbac = _function_contract(bind, _RBAC_SIGNATURE)
    delete_guard = _function_contract(bind, _DELETE_SIGNATURE)
    if rbac["owner_name"] != _SECURITY_OWNER or not rbac["security_definer"]:
        raise RuntimeError("8192 forward RBAC function owner/security contract failed.")
    if "FROM public.gym_owners" not in rbac["function_definition"]:
        raise RuntimeError("8192 forward RBAC function lost its bounded owner lookup.")
    if "NULLIF" not in rbac["function_definition"]:
        raise RuntimeError("8192 forward RBAC function is not empty-GUC safe.")
    if delete_guard["owner_name"] != "migration_owner" or delete_guard["security_definer"]:
        raise RuntimeError("8192 forward delete guard owner/security contract failed.")
    if "pg_advisory_xact_lock" not in delete_guard["function_definition"]:
        raise RuntimeError("8192 forward delete guard lost organization serialization.")
    if (
        "FROM public.organizations" in delete_guard["function_definition"]
        or "FOR UPDATE" in delete_guard["function_definition"]
    ):
        raise RuntimeError("8192 forward delete guard retained tenant-root row locking.")

    if not _is_scoped_update_trigger(
        _trigger_definition(bind, "trg_branch_rbac"),
        function_name="enforce_branch_rbac",
        columns=("deleted_at", "branch_status"),
        requires_when=True,
    ):
        raise RuntimeError("8192 forward RBAC trigger is not column scoped.")
    if not _is_scoped_update_trigger(
        _trigger_definition(bind, "trg_prevent_critical_branch_deletion"),
        function_name="prevent_critical_branch_deletion",
        columns=("deleted_at",),
        requires_when=True,
    ):
        raise RuntimeError("8192 forward delete trigger is not column scoped.")

    privilege_row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.has_column_privilege(
                    'app_security_owner', 'public.gym_owners', 'id', 'SELECT'
                ) AS owner_id_select,
                pg_catalog.has_column_privilege(
                    'app_security_owner', 'public.gym_owners', 'org_id', 'SELECT'
                ) AS owner_org_select,
                pg_catalog.has_column_privilege(
                    'app_security_owner', 'public.gym_owners', 'role', 'SELECT'
                ) AS owner_role_select,
                pg_catalog.has_table_privilege(
                    'app_runtime', 'public.gym_owners', 'SELECT'
                ) AS runtime_table_select,
                pg_catalog.has_column_privilege(
                    'app_runtime', 'public.gym_owners', 'id', 'SELECT'
                ) AS runtime_id_select,
                pg_catalog.has_column_privilege(
                    'app_runtime', 'public.gym_owners', 'org_id', 'SELECT'
                ) AS runtime_org_select,
                pg_catalog.has_column_privilege(
                    'app_runtime', 'public.gym_owners', 'role', 'SELECT'
                ) AS runtime_role_select,
                pg_catalog.has_schema_privilege(
                    'app_security_owner', 'public', 'CREATE'
                ) AS security_owner_create
            """
        )
    ).mappings().one()
    if not all(
        privilege_row[name]
        for name in ("owner_id_select", "owner_org_select", "owner_role_select")
    ):
        raise RuntimeError("8192 app_security_owner column SELECT contract is incomplete.")
    if any(
        privilege_row[name]
        for name in (
            "runtime_table_select",
            "runtime_id_select",
            "runtime_org_select",
            "runtime_role_select",
            "security_owner_create",
        )
    ):
        raise RuntimeError(
            "8192 leaked gym_owners/public-schema capability outside the approved boundary."
        )


def _drop_forward_objects() -> None:
    op.execute("DROP TRIGGER trg_branch_rbac ON public.org_branch_state")
    op.execute("DROP FUNCTION public.enforce_branch_rbac()")
    op.execute(
        "DROP TRIGGER trg_prevent_critical_branch_deletion ON public.org_branch_state"
    )
    op.execute("DROP FUNCTION public.prevent_critical_branch_deletion()")
    op.execute(
        """
        REVOKE SELECT (id, org_id, role)
        ON TABLE public.gym_owners
        FROM app_security_owner
        """
    )


def _create_predecessor_objects() -> None:
    # Exact 0005 definition as amended by 0007_fix_rbac_trigger.
    op.execute(
        """
        CREATE FUNCTION public.enforce_branch_rbac()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
          actor_role TEXT;
          actor_id UUID;
        BEGIN
          actor_id := current_setting('app.current_user_id', true)::UUID;

          IF actor_id IS NULL THEN
            RETURN NEW;
          END IF;

          SELECT role::TEXT INTO actor_role FROM gym_owners WHERE id = actor_id AND org_id = OLD.org_id;

          IF actor_role IS NULL THEN
            RAISE EXCEPTION 'Actor % has no membership in org %', actor_id, OLD.org_id;
          END IF;

          IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can soft-delete branches';
          END IF;

          IF OLD.branch_status IS DISTINCT FROM NEW.branch_status AND actor_role NOT IN ('owner', 'admin') THEN
            RAISE EXCEPTION 'Insufficient privileges: staff cannot change branch status';
          END IF;

          IF OLD.branch_status = 'archived' AND NEW.branch_status = 'active' AND actor_role NOT IN ('owner') THEN
            RAISE EXCEPTION 'Insufficient privileges: only owners can restore archived branches';
          END IF;

          RETURN NEW;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_branch_rbac
        BEFORE UPDATE ON public.org_branch_state
        FOR EACH ROW EXECUTE FUNCTION public.enforce_branch_rbac()
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.prevent_critical_branch_deletion()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
          active_count INTEGER;
        BEGIN
          PERFORM 1 FROM organizations WHERE id = OLD.org_id FOR UPDATE;
          SELECT COUNT(*) INTO active_count FROM org_branch_state WHERE org_id = OLD.org_id AND deleted_at IS NULL;

          IF OLD.is_primary = TRUE AND NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
            RAISE EXCEPTION 'Cannot delete the primary branch';
          END IF;

          IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL AND active_count <= 1 THEN
            RAISE EXCEPTION 'Cannot delete the last branch of an organization';
          END IF;

          RETURN NEW;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prevent_critical_branch_deletion
        BEFORE UPDATE ON public.org_branch_state
        FOR EACH ROW EXECUTE FUNCTION public.prevent_critical_branch_deletion()
        """
    )


def _verify_predecessor(bind) -> None:
    rbac = _function_contract(bind, _RBAC_SIGNATURE)
    delete_guard = _function_contract(bind, _DELETE_SIGNATURE)
    if rbac["owner_name"] != "migration_owner" or rbac["security_definer"]:
        raise RuntimeError("8192 downgrade failed to restore predecessor RBAC function.")
    if delete_guard["owner_name"] != "migration_owner" or delete_guard["security_definer"]:
        raise RuntimeError("8192 downgrade failed to restore predecessor delete guard.")
    if "FROM gym_owners" not in rbac["function_definition"]:
        raise RuntimeError("8192 downgrade RBAC body does not match predecessor.")
    if (
        "FROM organizations" not in delete_guard["function_definition"]
        or "FOR UPDATE" not in delete_guard["function_definition"]
    ):
        raise RuntimeError("8192 downgrade delete-guard body does not match predecessor.")
    if not _is_unscoped_update_trigger(
        _trigger_definition(bind, "trg_branch_rbac"),
        "enforce_branch_rbac",
    ):
        raise RuntimeError("8192 downgrade did not restore predecessor RBAC trigger.")
    if not _is_unscoped_update_trigger(
        _trigger_definition(bind, "trg_prevent_critical_branch_deletion"),
        "prevent_critical_branch_deletion",
    ):
        raise RuntimeError("8192 downgrade did not restore predecessor delete trigger.")

    leaked_columns = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.column_privileges
                WHERE table_schema = 'public'
                  AND table_name = 'gym_owners'
                  AND grantee = 'app_security_owner'
                  AND privilege_type = 'SELECT'
                  AND column_name IN ('id', 'org_id', 'role')
            )
            """
        )
    ).scalar_one()
    if leaked_columns:
        raise RuntimeError("8192 downgrade leaked revision-owned gym_owners privileges.")


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    _drop_predecessor_objects()
    _create_forward_delete_guard()
    _create_forward_rbac_guard()
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_migration_owner(bind)
    _verify_forward(bind)
    _drop_forward_objects()
    _create_predecessor_objects()
    _verify_predecessor(bind)
