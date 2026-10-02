"""PAY-24-B current-generation refund provider-admission bridge.

Revision ID: zz97d8e9f0a69
Revises: zz87d8e9f0a68
Create Date: 2026-10-01

Expose one bounded refund-execution-only bridge for finance_refund_runtime.
The bridge reads the current durable activation generation and delegates all
Stage, egress, tenant, release, capability, and replay checks to the certified
PAY-24-A admission authority.  It neither performs provider I/O nor changes
the independently certified checkout bridge.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz97d8e9f0a69"
down_revision = "zz87d8e9f0a68"
branch_labels = None
depends_on = None


_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_REFUND_RUNTIME = "finance_refund_runtime"
_PAYMENT_RUNTIME = "finance_payment_runtime"

_FUNCTION = (
    "app_secure.pay24b_request_current_refund_admission(text,text,integer)"
)
_FUNCTION_NAME = "pay24b_request_current_refund_admission"
_FUNCTION_ARGTYPES = "text, text, integer"
_PREDECESSOR = (
    "app_secure.pay24a_request_provider_admission(bigint,text,text,text,integer)"
)
_PREDECESSOR_NAME = "pay24a_request_provider_admission"
_PREDECESSOR_ARGTYPES = "bigint, text, text, text, integer"
_CHECKOUT_BRIDGE = (
    "app_secure.pay24b_request_current_provider_admission(text,text,text,integer)"
)
_CHECKOUT_BRIDGE_NAME = "pay24b_request_current_provider_admission"
_CHECKOUT_BRIDGE_ARGTYPES = "text, text, text, integer"

_RUNTIME_ROLES = (
    "app_runtime",
    "worker_runtime",
    _PAYMENT_RUNTIME,
    _REFUND_RUNTIME,
    "finance_reconciliation_runtime",
    "finance_config_runtime",
    "finance_read_runtime",
    "finance_maintenance_runtime",
    "lifecycle_maintenance_runtime",
)

_ISOLATED_ROLE_EDGES = (
    ("app_runtime", _REFUND_RUNTIME),
    ("worker_runtime", _REFUND_RUNTIME),
    (_PAYMENT_RUNTIME, _REFUND_RUNTIME),
    (_REFUND_RUNTIME, _PAYMENT_RUNTIME),
    ("finance_reconciliation_runtime", _REFUND_RUNTIME),
    (_REFUND_RUNTIME, "finance_reconciliation_runtime"),
)


def _identity(bind) -> tuple[str, str]:
    row = bind.execute(
        sa.text("SELECT session_user::text,current_user::text")
    ).one()
    return str(row[0]), str(row[1])


def _require_reduced_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
                   rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role
            """
        ),
        {"role": role},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            f"PAY24B refund bridge missing externally managed role: {role}"
        )
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY24B refund bridge login posture drift: {role}")
    for attribute in (
        "rolsuper",
        "rolinherit",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        if bool(row[attribute]):
            raise RuntimeError(
                "PAY24B refund bridge reduced-role drift: "
                f"{role}.{attribute}"
            )


def _require_migration_owner(bind) -> None:
    if _identity(bind) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError(
            "PAY24B refund bridge requires "
            "session_user=current_user=migration_owner"
        )


def _can_set_security_owner(bind) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.pg_has_role("
                ":member,:target,'SET')"
            ),
            {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER},
        ).scalar_one()
    )


def _enter_security_owner(bind) -> None:
    _require_migration_owner(bind)
    if not _can_set_security_owner(bind):
        raise RuntimeError(
            "PAY24B refund bridge migration_owner cannot "
            "SET ROLE app_security_owner"
        )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    if _identity(bind) != (_MIGRATION_OWNER, _SECURITY_OWNER):
        raise RuntimeError(
            "PAY24B refund bridge failed to enter bounded "
            "app_security_owner context"
        )


def _reset_role(bind) -> None:
    bind.execute(sa.text("RESET ROLE"))
    _require_migration_owner(bind)


def _function_observation(
    bind,
    *,
    name: str,
    argtypes: str,
) -> dict | None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                p.oid AS function_oid,
                owner_role.rolname AS owner,
                p.prosecdef AS security_definer,
                p.provolatile::text AS volatility,
                pg_catalog.oidvectortypes(p.proargtypes) AS argument_types,
                coalesce(array_to_string(p.proconfig,','),'') AS config,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.aclexplode(
                        coalesce(
                            p.proacl,
                            pg_catalog.acldefault('f',p.proowner)
                        )
                    ) AS acl
                    WHERE acl.grantee=0
                      AND acl.privilege_type='EXECUTE'
                ) AS public_execute,
                pg_catalog.pg_get_functiondef(p.oid) AS definition
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid=p.proowner
            WHERE n.nspname='app_secure'
              AND p.proname=:name
              AND p.prokind='f'
            """
        ),
        {"name": name},
    ).mappings().all()
    if len(rows) > 1:
        raise RuntimeError(
            "PAY24B refund bridge overloaded function identity: "
            f"app_secure.{name}"
        )
    if not rows:
        return None
    if rows[0]["argument_types"] != argtypes:
        raise RuntimeError(
            "PAY24B refund bridge function signature drift: "
            f"app_secure.{name}({argtypes})"
        )
    return dict(rows[0])


def _has_execute(bind, role: str, function_oid: int) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_function_privilege("
                ":role,CAST(:function_oid AS oid),'EXECUTE')"
            ),
            {"role": role, "function_oid": function_oid},
        ).scalar_one()
    )


def _relation_oid(bind, schema: str, relation: str) -> int:
    rows = bind.execute(
        sa.text(
            """
            SELECT c.oid
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid=c.relnamespace
            WHERE n.nspname=:schema
              AND c.relname=:relation
              AND c.relkind IN ('r','p')
            """
        ),
        {"schema": schema, "relation": relation},
    ).all()
    if len(rows) != 1:
        raise RuntimeError(
            "PAY24B refund bridge relation identity drift: "
            f"{schema}.{relation}"
        )
    return int(rows[0][0])


def _require_role_isolation(bind) -> None:
    for member, target in _ISOLATED_ROLE_EDGES:
        reachable = bool(
            bind.execute(
                sa.text(
                    "SELECT "
                    "pg_catalog.pg_has_role(:member,:target,'MEMBER') "
                    "OR pg_catalog.pg_has_role(:member,:target,'SET')"
                ),
                {"member": member, "target": target},
            ).scalar_one()
        )
        if reachable:
            raise RuntimeError(
                "PAY24B refund bridge runtime isolation drift: "
                f"{member} -> {target}"
            )

    for role in _RUNTIME_ROLES:
        reaches_owner = bool(
            bind.execute(
                sa.text(
                    "SELECT "
                    "pg_catalog.pg_has_role(:role,:owner,'MEMBER') "
                    "OR pg_catalog.pg_has_role(:role,:owner,'SET')"
                ),
                {"role": role, "owner": _SECURITY_OWNER},
            ).scalar_one()
        )
        if reaches_owner:
            raise RuntimeError(
                "PAY24B refund bridge runtime may reach security owner: "
                f"{role}"
            )


def _require_common_prerequisites(bind) -> None:
    _require_reduced_role(bind, _MIGRATION_OWNER, login=True)
    _require_reduced_role(bind, _SECURITY_OWNER)
    for role in _RUNTIME_ROLES:
        _require_reduced_role(bind, role)

    _require_migration_owner(bind)
    if not _can_set_security_owner(bind):
        raise RuntimeError(
            "PAY24B refund bridge requires bounded migration_owner "
            "SET edge to app_security_owner"
        )
    if bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _MIGRATION_OWNER},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY24B refund bridge migration_owner must remain "
            "app_secure-blind"
        )
    if not bool(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.has_schema_privilege("
                ":role,'app_secure','USAGE')"
            ),
            {"role": _REFUND_RUNTIME},
        ).scalar_one()
    ):
        raise RuntimeError(
            "PAY24B refund bridge requires inherited refund-runtime "
            "app_secure USAGE"
        )
    _require_role_isolation(bind)


def _require_predecessor_contract(bind) -> None:
    predecessor = _function_observation(
        bind,
        name=_PREDECESSOR_NAME,
        argtypes=_PREDECESSOR_ARGTYPES,
    )
    if (
        predecessor is None
        or predecessor["owner"] != _SECURITY_OWNER
        or not bool(predecessor["security_definer"])
        or "search_path=pg_catalog" not in predecessor["config"]
        or "row_security=on" not in predecessor["config"]
        or bool(predecessor["public_execute"])
    ):
        raise RuntimeError(
            "PAY24B refund bridge PAY24-A admission predecessor drift"
        )
    for role in (_PAYMENT_RUNTIME, _REFUND_RUNTIME):
        if not _has_execute(bind, role, int(predecessor["function_oid"])):
            raise RuntimeError(
                "PAY24B refund bridge predecessor authority missing: "
                f"{role}"
            )

    checkout = _function_observation(
        bind,
        name=_CHECKOUT_BRIDGE_NAME,
        argtypes=_CHECKOUT_BRIDGE_ARGTYPES,
    )
    if (
        checkout is None
        or checkout["owner"] != _SECURITY_OWNER
        or not bool(checkout["security_definer"])
        or not _has_execute(
            bind, _PAYMENT_RUNTIME, int(checkout["function_oid"])
        )
        or _has_execute(
            bind, _REFUND_RUNTIME, int(checkout["function_oid"])
        )
    ):
        raise RuntimeError(
            "PAY24B refund bridge checkout authority isolation drift"
        )


def _require_bridge_absent(bind) -> None:
    if _function_observation(
        bind,
        name=_FUNCTION_NAME,
        argtypes=_FUNCTION_ARGTYPES,
    ) is not None:
        raise RuntimeError(
            "PAY24B current refund admission bridge already exists"
        )


def _require_bridge_installed(bind) -> None:
    observed = _function_observation(
        bind,
        name=_FUNCTION_NAME,
        argtypes=_FUNCTION_ARGTYPES,
    )
    if (
        observed is None
        or observed["owner"] != _SECURITY_OWNER
        or not bool(observed["security_definer"])
        or observed["volatility"] != "v"
        or "search_path=pg_catalog" not in observed["config"]
        or "row_security=on" not in observed["config"]
        or bool(observed["public_execute"])
    ):
        raise RuntimeError(
            "PAY24B current refund admission bridge security drift"
        )

    definition = str(observed["definition"])
    for required in (
        "'finance_refund_runtime'",
        "'refund_execution'",
        "pay24a_request_provider_admission",
    ):
        if required not in definition:
            raise RuntimeError(
                "PAY24B current refund admission bridge body drift: "
                f"{required}"
            )
    if "p_capability" in definition or "'finance_payment_runtime'" in definition:
        raise RuntimeError(
            "PAY24B current refund admission bridge is not refund-only"
        )

    for role in _RUNTIME_ROLES:
        expected = role == _REFUND_RUNTIME
        if (
            _has_execute(bind, role, int(observed["function_oid"]))
            is not expected
        ):
            raise RuntimeError(
                "PAY24B current refund admission bridge ACL drift: "
                f"{role}"
            )

    for relation in (
        "payment_activation_authority",
        "provider_admission_leases",
    ):
        relation_oid = _relation_oid(bind, "finance", relation)
        for role in (_PAYMENT_RUNTIME, _REFUND_RUNTIME):
            direct_dml = bool(
                bind.execute(
                    sa.text(
                        "SELECT "
                        "pg_catalog.has_table_privilege("
                        ":role,CAST(:relation_oid AS oid),'SELECT') "
                        "OR pg_catalog.has_table_privilege("
                        ":role,CAST(:relation_oid AS oid),'INSERT') "
                        "OR pg_catalog.has_table_privilege("
                        ":role,CAST(:relation_oid AS oid),'UPDATE') "
                        "OR pg_catalog.has_table_privilege("
                        ":role,CAST(:relation_oid AS oid),'DELETE')"
                    ),
                    {"role": role, "relation_oid": relation_oid},
                ).scalar_one()
            )
            if direct_dml:
                raise RuntimeError(
                    "PAY24B refund bridge leaked direct Finance DML: "
                    f"{role} -> finance.{relation}"
                )

    _require_role_isolation(bind)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")

    _require_common_prerequisites(bind)
    _require_predecessor_contract(bind)
    _require_bridge_absent(bind)

    _enter_security_owner(bind)
    op.execute(
        r"""
        CREATE FUNCTION app_secure.pay24b_request_current_refund_admission(
            p_logical_operation_id text,
            p_operation_sha text,
            p_lease_seconds integer
        )
        RETURNS TABLE(
            admission_id uuid,
            activation_generation bigint,
            organization_id uuid,
            capability text,
            logical_operation_id text,
            lease_expires_at timestamptz,
            state text
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path=pg_catalog
        SET row_security=on
        AS $function$
        DECLARE
            v_generation bigint;
        BEGIN
            IF NOT pg_catalog.pg_has_role(
                session_user,'finance_refund_runtime','MEMBER'
            ) THEN
                RAISE EXCEPTION
                    'PAY-24-B refund admission requires finance_refund_runtime'
                    USING ERRCODE='42501';
            END IF;

            SELECT authority.generation
              INTO STRICT v_generation
              FROM finance.payment_activation_authority AS authority
             WHERE authority.singleton;

            RETURN QUERY
            SELECT requested.admission_id,
                   requested.activation_generation,
                   requested.organization_id,
                   requested.capability,
                   requested.logical_operation_id,
                   requested.lease_expires_at,
                   requested.state
              FROM app_secure.pay24a_request_provider_admission(
                   v_generation,
                   'refund_execution',
                   p_logical_operation_id,
                   p_operation_sha,
                   p_lease_seconds
              ) AS requested;
        END
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    for role in _RUNTIME_ROLES:
        if role != _REFUND_RUNTIME:
            op.execute(
                f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} FROM {role}"
            )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO finance_refund_runtime"
    )
    # RESET is success-only. PostgreSQL transaction rollback clears LOCAL role
    # state after a failed DDL statement without masking the original error.
    _reset_role(bind)

    _require_bridge_installed(bind)
    _require_predecessor_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")

    _require_common_prerequisites(bind)
    _require_predecessor_contract(bind)
    _require_bridge_installed(bind)

    _enter_security_owner(bind)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {_FUNCTION} FROM finance_refund_runtime"
    )
    op.execute(f"DROP FUNCTION {_FUNCTION}")
    _reset_role(bind)

    _require_bridge_absent(bind)
    _require_predecessor_contract(bind)
    _require_role_isolation(bind)
