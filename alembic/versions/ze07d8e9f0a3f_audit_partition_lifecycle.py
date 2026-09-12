"""Durable branch audit partition lifecycle authority.

Revision ID: ze07d8e9f0a3f
Revises: zd07d8e9f0a3e

The historical branch_audit_log lineage seeded fixed monthly partitions only
through August 2026.  This migration installs a durable, narrowly bounded
maintenance authority without granting CREATE(public) to any runtime role.

Authority split:

* app_private.maintain_branch_audit_partitions_internal()
    SECURITY DEFINER, owner migration_owner.
    It owns the actual partition DDL because migration_owner already owns
    public.branch_audit_log and already owns CREATE(public).

* app_secure.maintain_branch_audit_partitions()
    SECURITY DEFINER, owner app_security_owner.
    It is the only runtime-facing capability and accepts no arguments.

* lifecycle_maintenance_runtime
    receives EXECUTE only on the app_secure wrapper.

migration_owner additionally receives EXECUTE only on the immutable audit
trigger function because PostgreSQL must clone that trigger while attaching
new partitions.

No runtime/security role receives CREATE(public).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ze07d8e9f0a3f"
down_revision = "zd07d8e9f0a3e"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"

_PARENT = "public.branch_audit_log"

_TRIGGER_FUNCTION = (
    "app_private.raise_immutable_audit_violation()"
)

_INTERNAL_FUNCTION = (
    "app_private."
    "maintain_branch_audit_partitions_internal()"
)

_WRAPPER_FUNCTION = (
    "app_secure.maintain_branch_audit_partitions()"
)

_PARTITION_COMMENT = (
    "doers:ze07d8e9f0a3f:"
    "branch-audit-partition-lifecycle"
)

_LOCK_KEY = 684298311372640117


def _identity(bind) -> dict[str, str]:
    return dict(
        bind.execute(
            sa.text(
                "SELECT "
                "session_user::text AS session_user_name, "
                "current_user::text AS current_user_name"
            )
        ).mappings().one()
    )


def _require_migration_owner(bind) -> None:
    observed = _identity(bind)
    expected = {
        "session_user_name": _MIGRATION_OWNER,
        "current_user_name": _MIGRATION_OWNER,
    }
    if observed != expected:
        raise RuntimeError(
            "ze07d8e9f0a3f requires "
            "session_user=current_user=migration_owner; "
            f"observed {observed!r}"
        )


def _can_set_security_owner(bind) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                "session_user, "
                "'app_security_owner', "
                "'SET'"
                ")"
            )
        ).scalar_one()
    )


def _run_as_security_owner(bind, statements) -> None:
    _require_migration_owner(bind)

    if not _can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE "
            "app_security_owner"
        )

    if isinstance(statements, str):
        statements = (statements,)

    bind.execute(
        sa.text(
            "SET LOCAL ROLE app_security_owner"
        )
    )

    observed = _identity(bind)

    if observed["session_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE changed session_user"
        )

    if observed["current_user_name"] != _SECURITY_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE did not enter "
            "app_security_owner"
        )

    for statement in statements:
        bind.exec_driver_sql(statement)

    bind.execute(sa.text("RESET ROLE"))
    _require_migration_owner(bind)


def _function_oid(
    bind,
    schema_name: str,
    function_name: str,
) -> int | None:
    value = bind.execute(
        sa.text(
            """
            SELECT procedure.oid
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = :schema_name
              AND procedure.proname = :function_name
              AND pg_catalog.pg_get_function_identity_arguments(
                    procedure.oid
                  ) = ''
            """
        ),
        {
            "schema_name": schema_name,
            "function_name": function_name,
        },
    ).scalar_one_or_none()

    return None if value is None else int(value)


def _function_exists(bind, signature: str) -> bool:
    if not signature.endswith("()"):
        raise RuntimeError(
            f"unsupported function signature {signature!r}"
        )

    qualified_name = signature[:-2]

    if qualified_name.count(".") != 1:
        raise RuntimeError(
            f"unsupported function signature {signature!r}"
        )

    schema_name, function_name = qualified_name.split(
        ".",
        1,
    )

    return (
        _function_oid(
            bind,
            schema_name,
            function_name,
        )
        is not None
    )


def _has_function_execute(
    bind,
    role_name: str,
    schema_name: str,
    function_name: str,
) -> bool:
    function_oid = _function_oid(
        bind,
        schema_name,
        function_name,
    )

    if function_oid is None:
        raise RuntimeError(
            "required function is missing: "
            f"{schema_name}.{function_name}()"
        )

    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_function_privilege(
                    CAST(:role_name AS text),
                    CAST(:function_oid AS oid),
                    'EXECUTE'
                )
                """
            ),
            {
                "role_name": role_name,
                "function_oid": function_oid,
            },
        ).scalar_one()
    )


def _parent_contract(bind) -> tuple[str, str]:
    row = bind.execute(
        sa.text(
            """
            SELECT
                relation.relkind::text,
                pg_catalog.pg_get_userbyid(
                    relation.relowner
                )::text
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'branch_audit_log'
            """
        )
    ).one_or_none()

    if row is None:
        raise RuntimeError(
            "public.branch_audit_log is missing"
        )

    return str(row[0]), str(row[1])


def _marked_partitions(bind) -> tuple[str, ...]:
    rows = bind.execute(
        sa.text(
            """
            SELECT child.relname::text
            FROM pg_catalog.pg_inherits AS inheritance
            JOIN pg_catalog.pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_catalog.pg_namespace AS parent_namespace
              ON parent_namespace.oid = parent.relnamespace
            JOIN pg_catalog.pg_class AS child
              ON child.oid = inheritance.inhrelid
            JOIN pg_catalog.pg_namespace AS child_namespace
              ON child_namespace.oid = child.relnamespace
            WHERE parent_namespace.nspname = 'public'
              AND parent.relname = 'branch_audit_log'
              AND child_namespace.nspname = 'public'
              AND pg_catalog.obj_description(
                    child.oid,
                    'pg_class'
                  ) = :marker
            ORDER BY child.relname DESC
            """
        ),
        {"marker": _PARTITION_COMMENT},
    ).scalars().all()

    return tuple(str(row) for row in rows)


def _partition_is_empty(bind, relation_name: str) -> bool:
    if not relation_name.startswith(
        "branch_audit_log_y"
    ):
        raise RuntimeError(
            "unexpected marked partition name "
            f"{relation_name!r}"
        )

    quoted = (
        '"'
        + relation_name.replace('"', '""')
        + '"'
    )

    return not bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS("
                f"SELECT 1 FROM public.{quoted} LIMIT 1"
                ")"
            )
        ).scalar_one()
    )


_INTERNAL_SQL = f"""
CREATE OR REPLACE FUNCTION
app_private.maintain_branch_audit_partitions_internal()
RETURNS INTEGER
STRICT
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
LANGUAGE plpgsql
AS $function$
DECLARE
    v_parent_oid OID;
    v_offset INTEGER;
    v_start TIMESTAMPTZ;
    v_end TIMESTAMPTZ;
    v_name TEXT;
    v_relation REGCLASS;
    v_created INTEGER := 0;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        {_LOCK_KEY}
    );

    SELECT relation.oid
      INTO v_parent_oid
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     WHERE namespace.nspname = 'public'
       AND relation.relname = 'branch_audit_log'
       AND relation.relkind = 'p'
       AND pg_catalog.pg_get_userbyid(
             relation.relowner
           ) = 'migration_owner';

    IF v_parent_oid IS NULL THEN
        RAISE EXCEPTION
            'branch_audit_log partition parent contract is invalid'
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;

    FOR v_offset IN 0..2 LOOP
        v_start :=
            pg_catalog.date_trunc(
                'month',
                pg_catalog.clock_timestamp()
            )
            + pg_catalog.make_interval(
                months => v_offset
            );

        v_end :=
            v_start
            + pg_catalog.make_interval(months => 1);

        v_name :=
            'branch_audit_log_y'
            || pg_catalog.to_char(v_start, 'YYYY')
            || '_m'
            || pg_catalog.to_char(v_start, 'MM');

        v_relation :=
            pg_catalog.to_regclass(
                pg_catalog.format(
                    'public.%I',
                    v_name
                )
            );

        IF v_relation IS NULL THEN
            EXECUTE pg_catalog.format(
                'CREATE TABLE public.%I '
                'PARTITION OF public.branch_audit_log '
                'FOR VALUES FROM (%L) TO (%L)',
                v_name,
                v_start,
                v_end
            );

            EXECUTE pg_catalog.format(
                'COMMENT ON TABLE public.%I IS %L',
                v_name,
                '{_PARTITION_COMMENT}'
            );

            v_created := v_created + 1;
        ELSE
            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_inherits
                WHERE inhparent = v_parent_oid
                  AND inhrelid = v_relation
            ) THEN
                RAISE EXCEPTION
                    'audit partition name % exists '
                    'but is not attached to branch_audit_log',
                    v_name
                USING ERRCODE =
                    'object_not_in_prerequisite_state';
            END IF;
        END IF;
    END LOOP;

    RETURN v_created;
END;
$function$
"""


_WRAPPER_SQL = """
CREATE OR REPLACE FUNCTION
app_secure.maintain_branch_audit_partitions()
RETURNS INTEGER
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
LANGUAGE sql
AS $function$
    SELECT
        app_private.
        maintain_branch_audit_partitions_internal()
$function$
"""


def upgrade() -> None:
    bind = op.get_bind()

    if bind is None:
        raise RuntimeError(
            "ze07d8e9f0a3f requires online migration"
        )

    _require_migration_owner(bind)

    relkind, owner = _parent_contract(bind)

    if relkind != "p":
        raise RuntimeError(
            "branch_audit_log is not partitioned"
        )

    if owner != _MIGRATION_OWNER:
        raise RuntimeError(
            "branch_audit_log must be owned by "
            "migration_owner"
        )

    if _function_exists(
        bind,
        _INTERNAL_FUNCTION,
    ):
        raise RuntimeError(
            "audit partition internal function "
            "already exists"
        )

    if _function_exists(
        bind,
        _WRAPPER_FUNCTION,
    ):
        raise RuntimeError(
            "audit partition wrapper already exists"
        )

    trigger_owner = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(
                procedure.proowner
            )::text
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'app_private'
              AND procedure.proname =
                  'raise_immutable_audit_violation'
              AND pg_catalog.pg_get_function_identity_arguments(
                    procedure.oid
                  ) = ''
            """
        )
    ).scalar_one_or_none()

    if trigger_owner != _SECURITY_OWNER:
        raise RuntimeError(
            "immutable audit trigger owner "
            "contract is invalid"
        )

    already_can_execute_trigger = _has_function_execute(
        bind,
        _MIGRATION_OWNER,
        "app_private",
        "raise_immutable_audit_violation",
    )

    if already_can_execute_trigger:
        raise RuntimeError(
            "migration_owner unexpectedly already "
            "has immutable-trigger EXECUTE"
        )

    _run_as_security_owner(
        bind,
        (
            "GRANT EXECUTE ON FUNCTION "
            "app_private."
            "raise_immutable_audit_violation() "
            "TO migration_owner",
        ),
    )

    private_usage_before = bool(
        bind.execute(
            sa.text(
                "SELECT "
                "pg_catalog.has_schema_privilege("
                "'migration_owner', "
                "'app_private', "
                "'USAGE'"
                ")"
            )
        ).scalar_one()
    )

    private_create_before = bool(
        bind.execute(
            sa.text(
                "SELECT "
                "pg_catalog.has_schema_privilege("
                "'migration_owner', "
                "'app_private', "
                "'CREATE'"
                ")"
            )
        ).scalar_one()
    )

    private_usage_added = False
    private_create_added = False

    if not private_usage_before:
        _run_as_security_owner(
            bind,
            (
                "GRANT USAGE ON SCHEMA app_private "
                "TO migration_owner",
            ),
        )
        private_usage_added = True

    if not private_create_before:
        _run_as_security_owner(
            bind,
            (
                "GRANT CREATE ON SCHEMA app_private "
                "TO migration_owner",
            ),
        )
        private_create_added = True

    bind.exec_driver_sql(_INTERNAL_SQL)

    bind.exec_driver_sql(
        "REVOKE ALL ON FUNCTION "
        "app_private."
        "maintain_branch_audit_partitions_internal() "
        "FROM PUBLIC"
    )

    bind.exec_driver_sql(
        "GRANT EXECUTE ON FUNCTION "
        "app_private."
        "maintain_branch_audit_partitions_internal() "
        "TO app_security_owner"
    )

    if private_create_added:
        _run_as_security_owner(
            bind,
            (
                "REVOKE CREATE ON SCHEMA app_private "
                "FROM migration_owner",
            ),
        )

    if private_usage_added:
        _run_as_security_owner(
            bind,
            (
                "REVOKE USAGE ON SCHEMA app_private "
                "FROM migration_owner",
            ),
        )

    _run_as_security_owner(
        bind,
        (
            _WRAPPER_SQL,
            "REVOKE ALL ON FUNCTION "
            "app_secure."
            "maintain_branch_audit_partitions() "
            "FROM PUBLIC",
            "GRANT EXECUTE ON FUNCTION "
            "app_secure."
            "maintain_branch_audit_partitions() "
            "TO lifecycle_maintenance_runtime",
        ),
    )

    bind.execute(
        sa.text(
            "SET LOCAL ROLE app_security_owner"
        )
    )

    observed = _identity(bind)

    if observed["session_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError(
            "initial partition call changed session_user"
        )

    if observed["current_user_name"] != _SECURITY_OWNER:
        raise RuntimeError(
            "initial partition call did not enter "
            "app_security_owner"
        )

    created = int(
        bind.execute(
            sa.text(
                "SELECT "
                "app_secure."
                "maintain_branch_audit_partitions()"
            )
        ).scalar_one()
        or 0
    )

    bind.execute(sa.text("RESET ROLE"))
    _require_migration_owner(bind)

    if created < 1 or created > 3:
        raise RuntimeError(
            "unexpected initial audit partition "
            f"creation count: {created}"
        )

    lifecycle_execute = _has_function_execute(
        bind,
        "lifecycle_maintenance_runtime",
        "app_secure",
        "maintain_branch_audit_partitions",
    )

    if not lifecycle_execute:
        raise RuntimeError(
            "maintenance runtime lacks bounded "
            "partition capability"
        )

    for forbidden_role in (
        "app_runtime",
        "auth_runtime",
        "worker_runtime",
    ):
        forbidden_execute = _has_function_execute(
            bind,
            forbidden_role,
            "app_secure",
            "maintain_branch_audit_partitions",
        )

        if forbidden_execute:
            raise RuntimeError(
                f"{forbidden_role} unexpectedly has "
                "audit partition EXECUTE"
            )

        public_create = bool(
            bind.execute(
                sa.text(
                    "SELECT "
                    "pg_catalog.has_schema_privilege("
                    ":role_name, "
                    "'public', "
                    "'CREATE'"
                    ")"
                ),
                {"role_name": forbidden_role},
            ).scalar_one()
        )

        if public_create:
            raise RuntimeError(
                f"{forbidden_role} unexpectedly has "
                "CREATE(public)"
            )


def downgrade() -> None:
    bind = op.get_bind()

    if bind is None:
        raise RuntimeError(
            "ze07d8e9f0a3f requires online migration"
        )

    _require_migration_owner(bind)

    marked = _marked_partitions(bind)

    for relation_name in marked:
        if not _partition_is_empty(
            bind,
            relation_name,
        ):
            raise RuntimeError(
                "ze07 downgrade blocked: "
                "branch audit evidence exists in "
                f"{relation_name}"
            )

    _run_as_security_owner(
        bind,
        (
            "REVOKE EXECUTE ON FUNCTION "
            "app_secure."
            "maintain_branch_audit_partitions() "
            "FROM lifecycle_maintenance_runtime",
            "DROP FUNCTION "
            "app_secure."
            "maintain_branch_audit_partitions()",
        ),
    )

    private_usage_before = bool(
        bind.execute(
            sa.text(
                "SELECT "
                "pg_catalog.has_schema_privilege("
                "'migration_owner', "
                "'app_private', "
                "'USAGE'"
                ")"
            )
        ).scalar_one()
    )

    private_usage_added = False

    if not private_usage_before:
        _run_as_security_owner(
            bind,
            (
                "GRANT USAGE ON SCHEMA app_private "
                "TO migration_owner",
            ),
        )
        private_usage_added = True

    bind.exec_driver_sql(
        "REVOKE EXECUTE ON FUNCTION "
        "app_private."
        "maintain_branch_audit_partitions_internal() "
        "FROM app_security_owner"
    )

    bind.exec_driver_sql(
        "DROP FUNCTION "
        "app_private."
        "maintain_branch_audit_partitions_internal()"
    )

    if private_usage_added:
        _run_as_security_owner(
            bind,
            (
                "REVOKE USAGE ON SCHEMA app_private "
                "FROM migration_owner",
            ),
        )

    for relation_name in marked:
        quoted = (
            '"'
            + relation_name.replace('"', '""')
            + '"'
        )
        bind.exec_driver_sql(
            "DROP TABLE "
            f"public.{quoted} RESTRICT"
        )

    _run_as_security_owner(
        bind,
        (
            "REVOKE EXECUTE ON FUNCTION "
            "app_private."
            "raise_immutable_audit_violation() "
            "FROM migration_owner",
        ),
    )
