"""P3E: bound trial lifecycle transitions to the maintenance control plane.

Revision ID: m07d8e9f0a2d
Revises: l07d8e9f0a2c
Create Date: 2026-08-15

The legacy trial monitor performed a cross-tenant ORM scan under worker_runtime,
mixed database transitions with Redis invalidation, and attempted audit writes
through an invalid ORM field name. This revision replaces that authority model
with one bounded SECURITY DEFINER capability owned by app_security_owner.

The capability may only advance due trial rows according to database time:
active -> soft_locked and soft_locked -> hard_locked. It writes the corresponding
audit row in the same PostgreSQL transaction and returns only organization IDs
plus the new status so the maintenance task may perform best-effort cache
invalidation after commit. Ordinary workers and maintenance retain zero direct
trial/audit table ACL. The dedicated auth_runtime keeps only its exact inherited
onboarding contract: SELECT/INSERT on trial_subscriptions and INSERT on audit_logs;
it never receives EXECUTE on this maintenance capability.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "m07d8e9f0a2d"
down_revision = "l07d8e9f0a2c"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_MAINTENANCE_ROLE = "lifecycle_maintenance_runtime"
_BACKGROUND_ROLES = (_MAINTENANCE_ROLE, "worker_runtime")
_AUTH_ROLE = "auth_runtime"
_TRIAL_TABLE = "public.trial_subscriptions"
_AUDIT_TABLE = "public.audit_logs"
_AUTH_SENSITIVE_TABLE_ACL = {
    _TRIAL_TABLE: {"SELECT", "INSERT"},
    _AUDIT_TABLE: {"INSERT"},
}
_FUNCTION_NAME = "advance_trial_lifecycles"
_FUNCTION_SIGNATURE = "app_secure.advance_trial_lifecycles(integer)"
_TRIAL_SELECT = {"id", "organization_id", "status", "trial_end", "hard_lock_at"}
_TRIAL_UPDATE = {"status", "soft_locked_at", "hard_locked_at", "updated_at"}
_AUDIT_INSERT = {"id", "organization_id", "action", "metadata_json", "created_at"}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text, current_user::text,
               rolsuper, rolinherit, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
    """)).one()
    if row[0] != _MIGRATION_OWNER or row[1] != _MIGRATION_OWNER:
        raise RuntimeError("P3E trial lifecycle migration requires migration_owner")
    if any(bool(value) for value in row[2:]):
        raise RuntimeError("migration_owner violates the reduced role contract")


def _relation_state(bind, relation_name: str):
    schema, name = relation_name.split(".", 1)
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               relation.relrowsecurity,
               relation.relforcerowsecurity
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = relation.relowner
        WHERE namespace.nspname = :schema
          AND relation.relname = :name
          AND relation.relkind IN ('r', 'p')
    """), {"schema": schema, "name": name}).mappings().one_or_none()


def _direct_table_privileges(bind, relation_name: str, role_name: str) -> set[str]:
    return {
        str(value)
        for value in bind.execute(sa.text("""
            SELECT DISTINCT acl.privilege_type::text
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault('r', relation.relowner)
                )
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = acl.grantee
            WHERE relation.oid = pg_catalog.to_regclass(:relation)
              AND grantee.rolname = :role_name
        """), {
            "relation": relation_name,
            "role_name": role_name,
        }).scalars().all()
    }


def _column_privileges(
    bind,
    relation_name: str,
    role_name: str,
    privilege: str,
) -> set[str]:
    schema, name = relation_name.split(".", 1)
    return {
        str(value)
        for value in bind.execute(sa.text("""
            SELECT column_name::text
            FROM information_schema.column_privileges
            WHERE table_schema = :schema
              AND table_name = :name
              AND grantee = :role_name
              AND privilege_type = :privilege
            ORDER BY column_name
        """), {
            "schema": schema,
            "name": name,
            "role_name": role_name,
            "privilege": privilege,
        }).scalars().all()
    }


def _function_row(bind):
    return bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               procedure.prosecdef,
               procedure.provolatile::text AS volatility,
               procedure.proconfig,
               procedure.prosrc::text AS source,
               pg_catalog.has_function_privilege(
                   :maintenance_role,
                   procedure.oid,
                   'EXECUTE'
               ) AS maintenance_execute,
               pg_catalog.has_function_privilege(
                   'worker_runtime',
                   procedure.oid,
                   'EXECUTE'
               ) AS worker_execute,
               pg_catalog.has_function_privilege(
                   :auth_role,
                   procedure.oid,
                   'EXECUTE'
               ) AS auth_execute,
               pg_catalog.has_function_privilege(
                   'app_runtime',
                   procedure.oid,
                   'EXECUTE'
               ) AS app_execute,
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
               ) AS public_execute
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner
          ON owner.oid = procedure.proowner
        WHERE namespace.nspname = 'app_secure'
          AND procedure.proname = :function_name
          AND procedure.pronargs = 1
          AND procedure.prokind = 'f'
    """), {
        "function_name": _FUNCTION_NAME,
        "maintenance_role": _MAINTENANCE_ROLE,
        "auth_role": _AUTH_ROLE,
    }).mappings().one_or_none()


def _require_relation_contract(bind) -> None:
    for relation_name in (_TRIAL_TABLE, _AUDIT_TABLE):
        state = _relation_state(bind, relation_name)
        if state is None or state["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"{relation_name} ownership contract drifted")
        if bool(state["relrowsecurity"]) or bool(state["relforcerowsecurity"]):
            raise RuntimeError(
                f"P3E does not change predecessor RLS state for {relation_name}"
            )


def _require_no_direct_background_authority(bind) -> None:
    for role_name in _BACKGROUND_ROLES:
        for relation_name in (_TRIAL_TABLE, _AUDIT_TABLE):
            if _direct_table_privileges(bind, relation_name, role_name):
                raise RuntimeError(
                    f"{role_name} unexpectedly has table ACL on {relation_name}"
                )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                if _column_privileges(
                    bind,
                    relation_name,
                    role_name,
                    privilege,
                ):
                    raise RuntimeError(
                        f"{role_name} unexpectedly has {privilege} columns "
                        f"on {relation_name}"
                    )


def _require_exact_auth_onboarding_authority(bind) -> None:
    for relation_name, expected in _AUTH_SENSITIVE_TABLE_ACL.items():
        observed = _direct_table_privileges(bind, relation_name, _AUTH_ROLE)
        if observed != expected:
            raise RuntimeError(
                f"auth_runtime inherited onboarding ACL drift on {relation_name}: "
                f"expected={sorted(expected)!r}, observed={sorted(observed)!r}"
            )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            if _column_privileges(
                bind,
                relation_name,
                _AUTH_ROLE,
                privilege,
            ):
                raise RuntimeError(
                    f"auth_runtime has unexpected column-level {privilege} ACL "
                    f"on {relation_name}"
                )


def _require_predecessor(bind) -> None:
    _require_relation_contract(bind)
    _require_no_direct_background_authority(bind)
    _require_exact_auth_onboarding_authority(bind)
    if _function_row(bind) is not None:
        raise RuntimeError("P3E trial lifecycle capability already exists")

    if _direct_table_privileges(bind, _TRIAL_TABLE, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner has unexpected trial table ACL")
    if _direct_table_privileges(bind, _AUDIT_TABLE, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner has unexpected audit table ACL")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        if _column_privileges(bind, _TRIAL_TABLE, _SECURITY_OWNER, privilege):
            raise RuntimeError("app_security_owner predecessor trial ACL drift")
        if _column_privileges(bind, _AUDIT_TABLE, _SECURITY_OWNER, privilege):
            raise RuntimeError("app_security_owner predecessor audit ACL drift")


def _require_forward(bind) -> None:
    _require_relation_contract(bind)
    _require_no_direct_background_authority(bind)
    _require_exact_auth_onboarding_authority(bind)

    if _direct_table_privileges(bind, _TRIAL_TABLE, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner leaked trial table-wide ACL")
    if _direct_table_privileges(bind, _AUDIT_TABLE, _SECURITY_OWNER):
        raise RuntimeError("app_security_owner leaked audit table-wide ACL")

    if _column_privileges(
        bind, _TRIAL_TABLE, _SECURITY_OWNER, "SELECT"
    ) != _TRIAL_SELECT:
        raise RuntimeError("P3E trial SELECT column boundary drifted")
    if _column_privileges(
        bind, _TRIAL_TABLE, _SECURITY_OWNER, "UPDATE"
    ) != _TRIAL_UPDATE:
        raise RuntimeError("P3E trial UPDATE column boundary drifted")
    for privilege in ("INSERT", "DELETE"):
        if _column_privileges(bind, _TRIAL_TABLE, _SECURITY_OWNER, privilege):
            raise RuntimeError(f"unexpected trial {privilege} authority")

    if _column_privileges(
        bind, _AUDIT_TABLE, _SECURITY_OWNER, "INSERT"
    ) != _AUDIT_INSERT:
        raise RuntimeError("P3E audit INSERT column boundary drifted")
    for privilege in ("SELECT", "UPDATE", "DELETE"):
        if _column_privileges(bind, _AUDIT_TABLE, _SECURITY_OWNER, privilege):
            raise RuntimeError(f"unexpected audit {privilege} authority")

    row = _function_row(bind)
    if row is None:
        raise RuntimeError("P3E trial lifecycle capability is missing")
    if (
        row["owner_name"] != _SECURITY_OWNER
        or not bool(row["prosecdef"])
        or row["volatility"] != "v"
    ):
        raise RuntimeError("P3E trial function owner/security drifted")
    if set(row["proconfig"] or []) != {
        "search_path=pg_catalog",
        "row_security=on",
    }:
        raise RuntimeError("P3E trial function settings drifted")
    if (
        not bool(row["maintenance_execute"])
        or bool(row["worker_execute"])
        or bool(row["auth_execute"])
        or bool(row["app_execute"])
        or bool(row["public_execute"])
    ):
        raise RuntimeError("P3E trial function EXECUTE ACL drifted")

    source = " ".join(str(row["source"] or "").lower().split())
    for token in (
        "app.internal_maintenance",
        "is distinct from 'platform'",
        "p_batch_size < 1",
        "p_batch_size > 500",
        "for update skip locked",
        "status = 'active'",
        "status = 'soft_locked'",
        "status = 'hard_locked'",
        "insert into public.audit_logs",
        "trial_soft_locked",
        "trial_hard_locked",
    ):
        if token not in source:
            raise RuntimeError(
                f"P3E trial lifecycle function lost required token: {token}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    op.execute("""
        GRANT SELECT (id, organization_id, status, trial_end, hard_lock_at),
              UPDATE (status, soft_locked_at, hard_locked_at, updated_at)
        ON TABLE public.trial_subscriptions
        TO app_security_owner
    """)
    op.execute("""
        GRANT INSERT (id, organization_id, action, metadata_json, created_at)
        ON TABLE public.audit_logs
        TO app_security_owner
    """)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(r"""
        CREATE FUNCTION app_secure.advance_trial_lifecycles(
            p_batch_size integer
        ) RETURNS TABLE (
            organization_id uuid,
            new_status text
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = on
        AS $function$
        DECLARE
            v_row record;
            v_new_status text;
            v_now timestamptz;
        BEGIN
            IF pg_catalog.current_setting(
                   'app.internal_maintenance', true
               ) IS DISTINCT FROM 'platform'
               OR p_batch_size < 1
               OR p_batch_size > 500 THEN
                RAISE EXCEPTION 'invalid trial lifecycle command'
                    USING ERRCODE = '42501';
            END IF;

            v_now := pg_catalog.clock_timestamp();

            FOR v_row IN
                SELECT trial.id,
                       trial.organization_id,
                       trial.status,
                       trial.trial_end,
                       trial.hard_lock_at
                FROM public.trial_subscriptions AS trial
                WHERE (
                        trial.status = 'active'
                        AND trial.trial_end <= v_now
                      )
                   OR (
                        trial.status = 'soft_locked'
                        AND trial.hard_lock_at <= v_now
                      )
                ORDER BY
                    CASE
                        WHEN trial.status = 'soft_locked' THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN trial.status = 'soft_locked'
                        THEN trial.hard_lock_at
                        ELSE trial.trial_end
                    END,
                    trial.id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            LOOP
                IF v_row.status = 'active' THEN
                    v_new_status := 'soft_locked';
                    UPDATE public.trial_subscriptions AS trial
                    SET status = 'soft_locked',
                        soft_locked_at = v_now,
                        updated_at = v_now
                    WHERE trial.id = v_row.id
                      AND trial.status = 'active'
                      AND trial.trial_end <= v_now;
                ELSIF v_row.status = 'soft_locked' THEN
                    v_new_status := 'hard_locked';
                    UPDATE public.trial_subscriptions AS trial
                    SET status = 'hard_locked',
                        hard_locked_at = v_now,
                        updated_at = v_now
                    WHERE trial.id = v_row.id
                      AND trial.status = 'soft_locked'
                      AND trial.hard_lock_at <= v_now;
                ELSE
                    CONTINUE;
                END IF;

                IF FOUND THEN
                    INSERT INTO public.audit_logs (
                        id,
                        organization_id,
                        action,
                        metadata_json,
                        created_at
                    )
                    VALUES (
                        pg_catalog.gen_random_uuid(),
                        v_row.organization_id,
                        CASE
                            WHEN v_new_status = 'soft_locked'
                                THEN 'TRIAL_SOFT_LOCKED'
                            ELSE 'TRIAL_HARD_LOCKED'
                        END,
                        pg_catalog.jsonb_build_object(
                            'source', 'p3e_maintenance',
                            'previous_status', v_row.status,
                            'new_status', v_new_status
                        ),
                        v_now
                    );

                    organization_id := v_row.organization_id;
                    new_status := v_new_status;
                    RETURN NEXT;
                END IF;
            END LOOP;

            RETURN;
        END;
        $function$;
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE} TO {_MAINTENANCE_ROLE}"
    )
    op.execute("RESET ROLE")

    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(f"DROP FUNCTION {_FUNCTION_SIGNATURE}")
    op.execute("RESET ROLE")

    op.execute("""
        REVOKE UPDATE (status, soft_locked_at, hard_locked_at, updated_at),
               SELECT (id, organization_id, status, trial_end, hard_lock_at)
        ON TABLE public.trial_subscriptions
        FROM app_security_owner
    """)
    op.execute("""
        REVOKE INSERT (id, organization_id, action, metadata_json, created_at)
        ON TABLE public.audit_logs
        FROM app_security_owner
    """)

    _require_predecessor(bind)
