"""Harden lifecycle history correlation validation under FORCE RLS.

Revision ID: 8293a4b5c6d7
Revises: 718293a4b5c6
Create Date: 2026-08-11

Revision 708 correctly moved lifecycle append relations to FORCE RLS. The
predecessor history-correlation trigger remained SECURITY DEFINER under
``migration_owner``. FORCE RLS therefore made that definer subject to event RLS,
so it could become blind to a valid event flushed in the same transaction.

This revision keeps API and worker event-read privileges unchanged. A dedicated,
non-login ``app_security_owner`` trigger function receives only SELECT on the
three event columns required for validation plus one owner-only RLS policy. The
correlation must match the same branch and exact event timestamp. Validation is
a DEFERRABLE INITIALLY DEFERRED constraint trigger so event/history flush order
inside one atomic transaction is irrelevant while commit still fails closed.
PUBLIC execution is revoked on both internal trigger functions. ``migration_owner``
receives EXECUTE on the hardened function only for the exact trigger-creation
window, after which that capability is immediately revoked and verified absent.
Downgrade restores the exact predecessor trigger target and PUBLIC-execute
surface.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8293a4b5c6d7"
down_revision = "718293a4b5c6"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_EVENTS = "public.branch_lifecycle_events"
_HISTORY = "public.branch_status_history"
_PREDECESSOR_FUNCTION = "public.validate_history_correlation()"
_HARDENED_FUNCTION = "public.validate_history_correlation_hardened()"
_TRIGGER = "trg_validate_history_correlation"
_POLICY = "lifecycle_correlation_validator_event_read"
_REQUIRED_EVENT_COLUMNS = {"branch_id", "correlation_id", "emitted_at"}
_RUNTIME_ROLES = ("app_runtime", "auth_runtime", "worker_runtime")


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _function_contract(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(p.proowner)::text AS owner_name,
                p.prosecdef AS security_definer,
                p.prorettype = 'pg_catalog.trigger'::regtype AS returns_trigger,
                p.proconfig,
                p.prosrc::text AS source
            FROM pg_catalog.pg_proc AS p
            WHERE p.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).mappings().one_or_none()


def _trigger_contract(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                proc_ns.nspname || '.' || proc_data.proname || '()' AS function_name,
                trigger_data.tgdeferrable AS is_deferrable,
                trigger_data.tginitdeferred AS is_initially_deferred,
                pg_catalog.pg_get_triggerdef(trigger_data.oid, true)::text AS definition
            FROM pg_catalog.pg_trigger AS trigger_data
            JOIN pg_catalog.pg_proc AS proc_data
              ON proc_data.oid = trigger_data.tgfoid
            JOIN pg_catalog.pg_namespace AS proc_ns
              ON proc_ns.oid = proc_data.pronamespace
            WHERE trigger_data.tgrelid = CAST(:relation AS regclass)
              AND trigger_data.tgname = :trigger_name
              AND NOT trigger_data.tgisinternal
            """
        ),
        {"relation": _HISTORY, "trigger_name": _TRIGGER},
    ).mappings().one_or_none()


def _policy_contract(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                policy_data.polcmd::text AS command,
                policy_data.polpermissive AS permissive,
                role_data.rolname::text AS role_name,
                pg_catalog.pg_get_expr(
                    policy_data.polqual, policy_data.polrelid, true
                )::text AS using_expr,
                pg_catalog.pg_get_expr(
                    policy_data.polwithcheck, policy_data.polrelid, true
                )::text AS check_expr
            FROM pg_catalog.pg_policy AS policy_data
            JOIN pg_catalog.pg_roles AS role_data
              ON policy_data.polroles = ARRAY[role_data.oid]
            WHERE policy_data.polrelid = CAST(:relation AS regclass)
              AND policy_data.polname = :policy_name
            """
        ),
        {"relation": _EVENTS, "policy_name": _POLICY},
    ).mappings().one_or_none()


def _security_owner_event_select_columns(bind) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT a.attname::text
                FROM pg_catalog.pg_attribute AS a
                WHERE a.attrelid = CAST(:relation AS regclass)
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND pg_catalog.has_column_privilege(
                        CAST(:role_name AS name),
                        a.attrelid,
                        a.attnum,
                        'SELECT'
                  )
                ORDER BY a.attname
                """
            ),
            {"relation": _EVENTS, "role_name": _SECURITY_OWNER},
        ).scalars().all()
    )


def _public_execute(bind, signature: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT COALESCE(bool_or(
                acl_data.grantee = 0
                AND acl_data.privilege_type = 'EXECUTE'
            ), FALSE)
            FROM pg_catalog.pg_proc AS proc_data
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    proc_data.proacl,
                    pg_catalog.acldefault('f', proc_data.proowner)
                )
            ) AS acl_data
            WHERE proc_data.oid = pg_catalog.to_regprocedure(:signature)
            """,
            {"signature": signature},
        )
    )


def _role_execute(bind, role_name: str, signature: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT pg_catalog.has_function_privilege(
                CAST(:role_name AS name),
                pg_catalog.to_regprocedure(:signature),
                'EXECUTE'
            )
            """,
            {"role_name": role_name, "signature": signature},
        )
    )


def _has_public_schema_create(bind, role_name: str) -> bool:
    return bool(
        _scalar(
            bind,
            """
            SELECT pg_catalog.has_schema_privilege(
                CAST(:role_name AS name), 'public', 'CREATE'
            )
            """,
            {"role_name": role_name},
        )
    )


def _require_identity_and_roles(bind) -> None:
    identity = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if (
        identity["session_name"] != _MIGRATION_OWNER
        or identity["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("8293 requires session_user=current_user=migration_owner")
    if any(
        bool(identity[name])
        for name in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")

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
    ).one_or_none()
    if security_owner is None or any(bool(value) for value in security_owner):
        raise RuntimeError(
            "app_security_owner must remain NOLOGIN/NOINHERIT/NOBYPASSRLS"
        )
    if not _scalar(
        bind,
        "SELECT pg_catalog.pg_has_role(session_user, :role_name, 'SET')",
        {"role_name": _SECURITY_OWNER},
    ):
        raise RuntimeError("migration_owner cannot SET ROLE app_security_owner")

    for runtime_role in _RUNTIME_ROLES:
        if _scalar(
            bind,
            """
            SELECT pg_catalog.pg_has_role(:member, :owner, 'MEMBER')
                OR pg_catalog.pg_has_role(:member, :owner, 'SET')
            """,
            {"member": runtime_role, "owner": _SECURITY_OWNER},
        ):
            raise RuntimeError(
                f"{runtime_role} must not inherit or SET app_security_owner"
            )


def _require_rls_and_relation_owners(bind) -> None:
    for relation in (_EVENTS, _HISTORY):
        row = bind.execute(
            sa.text(
                """
                SELECT
                    pg_catalog.pg_get_userbyid(c.relowner)::text AS owner_name,
                    c.relrowsecurity,
                    c.relforcerowsecurity
                FROM pg_catalog.pg_class AS c
                WHERE c.oid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(f"8293 required relation missing: {relation}")
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"8293 relation owner drift: {relation}")
        if not row["relrowsecurity"] or not row["relforcerowsecurity"]:
            raise RuntimeError(f"8293 requires ENABLE+FORCE RLS on {relation}")


def _require_predecessor(bind) -> None:
    _require_identity_and_roles(bind)
    _require_rls_and_relation_owners(bind)

    predecessor = _function_contract(bind, _PREDECESSOR_FUNCTION)
    if predecessor is None:
        raise RuntimeError("8293 predecessor validator is missing")
    if (
        predecessor["owner_name"] != _MIGRATION_OWNER
        or not predecessor["security_definer"]
        or not predecessor["returns_trigger"]
    ):
        raise RuntimeError("8293 predecessor validator contract drifted")
    predecessor_source = predecessor["source"].lower()
    for token in (
        "branch_lifecycle_events",
        "correlation_id",
        "correlation_emitted_at",
    ):
        if token not in predecessor_source:
            raise RuntimeError(
                f"8293 predecessor validator body drifted: missing {token}"
            )

    trigger = _trigger_contract(bind)
    if trigger is None or trigger["function_name"] != _PREDECESSOR_FUNCTION:
        raise RuntimeError("8293 predecessor trigger target drifted")
    if trigger["is_deferrable"] or trigger["is_initially_deferred"]:
        raise RuntimeError("8293 predecessor trigger unexpectedly became deferred")

    if _function_contract(bind, _HARDENED_FUNCTION) is not None:
        raise RuntimeError("8293 hardened validator already exists")
    if _policy_contract(bind) is not None:
        raise RuntimeError("8293 validator RLS policy already exists")
    if _security_owner_event_select_columns(bind):
        raise RuntimeError(
            "8293 refuses pre-existing app_security_owner event SELECT capability"
        )
    if not _public_execute(bind, _PREDECESSOR_FUNCTION):
        raise RuntimeError("8293 predecessor PUBLIC EXECUTE contract drifted")
    if _has_public_schema_create(bind, _SECURITY_OWNER):
        raise RuntimeError("8293 refuses pre-existing public CREATE for app_security_owner")


def _create_hardened_validator() -> None:
    op.execute(
        """
        CREATE FUNCTION public.validate_history_correlation_hardened()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        SET row_security = on
        AS $validator$
        BEGIN
            IF NEW.correlation_id IS NULL THEN
                IF NEW.correlation_emitted_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'correlation_emitted_at requires correlation_id';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.correlation_emitted_at IS NULL THEN
                RAISE EXCEPTION
                    'correlation_id % requires correlation_emitted_at',
                    NEW.correlation_id;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM public.branch_lifecycle_events AS event_data
                WHERE event_data.correlation_id = NEW.correlation_id
                  AND event_data.branch_id = NEW.branch_id
                  AND event_data.emitted_at = NEW.correlation_emitted_at
                LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'correlation_id % has no exact lifecycle event for branch %',
                    NEW.correlation_id,
                    NEW.branch_id;
            END IF;
            RETURN NEW;
        END;
        $validator$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.validate_history_correlation_hardened() FROM PUBLIC"
    )


def _install_security_owner_boundary() -> None:
    # CREATE exists only for the owner-transfer statement and is removed again
    # in the same migration transaction.
    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.validate_history_correlation_hardened() "
        "OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")

    op.execute(
        "GRANT SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events TO app_security_owner"
    )
    op.execute(
        """
        CREATE POLICY lifecycle_correlation_validator_event_read
        ON public.branch_lifecycle_events
        FOR SELECT TO app_security_owner
        USING (TRUE)
        """
    )


def _replace_trigger(bind) -> None:
    op.execute(
        "REVOKE ALL ON FUNCTION public.validate_history_correlation() FROM PUBLIC"
    )
    op.execute(
        "DROP TRIGGER trg_validate_history_correlation ON public.branch_status_history"
    )

    # PostgreSQL requires the role creating a trigger to hold EXECUTE on the
    # trigger function. Keep this capability open only around CREATE TRIGGER;
    # transaction rollback closes the entire window on any failure.
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.validate_history_correlation_hardened() TO migration_owner"
    )
    if not _role_execute(bind, _MIGRATION_OWNER, _HARDENED_FUNCTION):
        raise RuntimeError("8293 failed to open bounded migration-owner EXECUTE")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_validate_history_correlation
        AFTER INSERT ON public.branch_status_history
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION public.validate_history_correlation_hardened()
        """
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.validate_history_correlation_hardened() FROM migration_owner"
    )
    if _role_execute(bind, _MIGRATION_OWNER, _HARDENED_FUNCTION):
        raise RuntimeError("8293 leaked migration-owner EXECUTE after trigger creation")


def _verify_forward(bind) -> None:
    _require_identity_and_roles(bind)
    _require_rls_and_relation_owners(bind)

    hardened = _function_contract(bind, _HARDENED_FUNCTION)
    if hardened is None:
        raise RuntimeError("8293 hardened validator disappeared")
    if (
        hardened["owner_name"] != _SECURITY_OWNER
        or not hardened["security_definer"]
        or not hardened["returns_trigger"]
    ):
        raise RuntimeError("8293 hardened validator owner/security contract failed")
    config = set(hardened["proconfig"] or ())
    if "row_security=on" not in config or not any(
        item.startswith("search_path=") for item in config
    ):
        raise RuntimeError("8293 hardened validator runtime config drifted")
    source = hardened["source"].lower()
    for token in (
        "event_data.correlation_id = new.correlation_id",
        "event_data.branch_id = new.branch_id",
        "event_data.emitted_at = new.correlation_emitted_at",
    ):
        if token not in source:
            raise RuntimeError(
                f"8293 hardened validator predicate drifted: missing {token}"
            )

    trigger = _trigger_contract(bind)
    if trigger is None or trigger["function_name"] != _HARDENED_FUNCTION:
        raise RuntimeError("8293 history trigger does not use hardened validator")
    definition = trigger["definition"].upper()
    if (
        not trigger["is_deferrable"]
        or not trigger["is_initially_deferred"]
        or "CREATE CONSTRAINT TRIGGER" not in definition
        or "AFTER INSERT" not in definition
        or "DEFERRABLE INITIALLY DEFERRED" not in definition
    ):
        raise RuntimeError("8293 history trigger is not deferred to transaction end")

    if _public_execute(bind, _HARDENED_FUNCTION):
        raise RuntimeError("8293 leaked PUBLIC EXECUTE on hardened validator")
    if _public_execute(bind, _PREDECESSOR_FUNCTION):
        raise RuntimeError("8293 left PUBLIC EXECUTE on detached predecessor validator")
    if _role_execute(bind, _MIGRATION_OWNER, _HARDENED_FUNCTION):
        raise RuntimeError("8293 leaked migration-owner EXECUTE on hardened validator")
    for runtime_role in _RUNTIME_ROLES:
        if _role_execute(bind, runtime_role, _HARDENED_FUNCTION):
            raise RuntimeError(
                f"8293 leaked hardened validator EXECUTE to {runtime_role}"
            )

    policy = _policy_contract(bind)
    if policy is None:
        raise RuntimeError("8293 validator RLS policy is missing")
    if (
        policy["command"] != "r"
        or not policy["permissive"]
        or policy["role_name"] != _SECURITY_OWNER
        or str(policy["using_expr"]).strip().lower() != "true"
        or policy["check_expr"] is not None
    ):
        raise RuntimeError("8293 validator RLS policy drifted")

    observed_columns = _security_owner_event_select_columns(bind)
    if observed_columns != _REQUIRED_EVENT_COLUMNS:
        raise RuntimeError(
            "8293 app_security_owner event SELECT drift: "
            f"observed={sorted(observed_columns)!r}"
        )
    if _has_public_schema_create(bind, _SECURITY_OWNER):
        raise RuntimeError("8293 leaked public CREATE to app_security_owner")


def upgrade() -> None:
    bind = op.get_bind()
    _require_predecessor(bind)
    _create_hardened_validator()
    _install_security_owner_boundary()
    _replace_trigger(bind)
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _verify_forward(bind)

    op.execute(
        "DROP TRIGGER trg_validate_history_correlation ON public.branch_status_history"
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_history_correlation
        BEFORE INSERT ON public.branch_status_history
        FOR EACH ROW
        EXECUTE FUNCTION public.validate_history_correlation()
        """
    )
    op.execute(
        "DROP POLICY lifecycle_correlation_validator_event_read "
        "ON public.branch_lifecycle_events"
    )
    op.execute(
        "REVOKE SELECT (branch_id, correlation_id, emitted_at) "
        "ON TABLE public.branch_lifecycle_events FROM app_security_owner"
    )

    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    bind.execute(
        sa.text(
            "DROP FUNCTION public.validate_history_correlation_hardened() RESTRICT"
        )
    )
    bind.execute(sa.text("RESET ROLE"))

    # The older 718 contract inherited PostgreSQL's default PUBLIC EXECUTE on
    # this trigger function. Restore that only when explicitly downgrading.
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.validate_history_correlation() TO PUBLIC"
    )

    _require_identity_and_roles(bind)
    _require_rls_and_relation_owners(bind)
    if _function_contract(bind, _HARDENED_FUNCTION) is not None:
        raise RuntimeError("8293 hardened validator remained after downgrade")
    trigger = _trigger_contract(bind)
    if trigger is None or trigger["function_name"] != _PREDECESSOR_FUNCTION:
        raise RuntimeError("8293 failed to restore predecessor trigger target")
    if trigger["is_deferrable"] or trigger["is_initially_deferred"]:
        raise RuntimeError("8293 failed to restore predecessor trigger timing")
    if _policy_contract(bind) is not None:
        raise RuntimeError("8293 validator policy remained after downgrade")
    if _security_owner_event_select_columns(bind):
        raise RuntimeError("8293 security-owner event SELECT remained after downgrade")
    if not _public_execute(bind, _PREDECESSOR_FUNCTION):
        raise RuntimeError("8293 failed to restore predecessor PUBLIC EXECUTE")
    if _has_public_schema_create(bind, _SECURITY_OWNER):
        raise RuntimeError("8293 security owner retained public CREATE after downgrade")
