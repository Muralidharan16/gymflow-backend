"""Establish least-privilege runtime access for organization addresses.

Revision ID: 6f708192a3b4
Revises: 5e6f708192a3
Create Date: 2026-08-10

The API exposes tenant-scoped organization-address reads and administrator-only
writes through the ordinary application database pool. The predecessor granted
address writes only to the legacy NOLOGIN ``branch_admin`` role, so production
requests could pass API authorization and FORCE-RLS checks yet still fail at the
base table ACL.

This revision gives ``app_runtime`` only SELECT/INSERT/UPDATE on the user-facing
address table. Trigger side effects remain internal: hardened SECURITY DEFINER
functions owned by ``app_security_owner`` write history/audit/outbox rows under
the caller's transaction-local tenant context with row_security=on. The runtime
role receives no direct ACL on those internal ledgers and no DELETE/TRUNCATE,
schema-create, ownership, or RLS-bypass capability.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "6f708192a3b4"
down_revision = "5e6f708192a3"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RUNTIME_ROLE = "app_runtime"

_ADDRESS_TABLE = "organization_addresses"
_HISTORY_TABLE = "branch_address_history"
_AUDIT_TABLE = "branch_address_audit_log"
_OUTBOX_TABLE = "address_change_outbox"

_HARDENED_INSERT_FUNCTION = "app_secure.snapshot_organization_address_on_insert()"
_HARDENED_UPDATE_FUNCTION = "app_secure.snapshot_organization_address_on_change()"
_AUDIT_INSERT_POLICY = "tenant_isolation_audit_insert"

_RUNTIME_PRIVILEGES = ("SELECT", "INSERT", "UPDATE")
_FORBIDDEN_RUNTIME_PRIVILEGES = ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_INTERNAL_PRIVILEGES = {
    _HISTORY_TABLE: ("SELECT", "INSERT", "UPDATE"),
    _AUDIT_TABLE: ("INSERT",),
    _OUTBOX_TABLE: ("INSERT",),
}


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _require_identity(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name,
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
    if row["session_user_name"] != _MIGRATION_OWNER or row["current_user_name"] != _MIGRATION_OWNER:
        raise RuntimeError("address runtime ACL migration requires migration_owner identity")
    if any(
        row[name]
        for name in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner attributes violate the reduced role contract")

    roles = bind.execute(
        sa.text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname IN (:security_owner, :runtime_role)
            """
        ),
        {"security_owner": _SECURITY_OWNER, "runtime_role": _RUNTIME_ROLE},
    ).mappings().all()
    by_name = {row["rolname"]: row for row in roles}
    if set(by_name) != {_SECURITY_OWNER, _RUNTIME_ROLE}:
        raise RuntimeError("address runtime ACL migration requires managed runtime/security roles")
    for role_name, role in by_name.items():
        if role["rolcanlogin"] or role["rolsuper"] or role["rolinherit"] or role["rolbypassrls"]:
            raise RuntimeError(f"managed role {role_name} violates the NOLOGIN/NOINHERIT/NOBYPASSRLS contract")

    edge = bind.execute(
        sa.text(
            """
            SELECT edge.admin_option, edge.inherit_option, edge.set_option
            FROM pg_catalog.pg_auth_members AS edge
            JOIN pg_catalog.pg_roles AS granted ON granted.oid = edge.roleid
            JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = edge.member
            WHERE granted.rolname = :granted_role
              AND member_role.rolname = :member_role
            """
        ),
        {"granted_role": _SECURITY_OWNER, "member_role": _MIGRATION_OWNER},
    ).mappings().all()
    if len(edge) != 1 or edge[0]["admin_option"] or edge[0]["inherit_option"] or not edge[0]["set_option"]:
        raise RuntimeError("migration_owner -> app_security_owner must be one SET-only membership edge")


def _relation_contract(bind, relation_name: str) -> dict[str, object]:
    row = bind.execute(
        sa.text(
            """
            SELECT
                owner.rolname::text AS owner_name,
                relation.relrowsecurity,
                relation.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
            WHERE namespace.nspname = 'public'
              AND relation.relname = :relation_name
              AND relation.relkind IN ('r', 'p')
            """
        ),
        {"relation_name": relation_name},
    ).mappings().all()
    if len(row) != 1:
        raise RuntimeError(f"missing required public relation {relation_name}")
    return dict(row[0])


def _direct_privileges(bind, role_name: str, relation_name: str) -> tuple[str, ...]:
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT acl.privilege_type::text AS privilege_type
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'public'
              AND relation.relname = :relation_name
              AND grantee.rolname = :role_name
            ORDER BY acl.privilege_type::text
            """
        ),
        {"relation_name": relation_name, "role_name": role_name},
    ).scalars().all()
    return tuple(rows)


def _policy_rows(bind, relation_name: str) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in bind.execute(
            sa.text(
                """
                SELECT
                    policy.polname::text AS policy_name,
                    policy.polcmd::text AS command,
                    pg_catalog.pg_get_expr(policy.polqual, policy.polrelid, true)::text AS using_expression,
                    pg_catalog.pg_get_expr(policy.polwithcheck, policy.polrelid, true)::text AS check_expression
                FROM pg_catalog.pg_policy AS policy
                JOIN pg_catalog.pg_class AS relation ON relation.oid = policy.polrelid
                JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = :relation_name
                ORDER BY policy.polname
                """
            ),
            {"relation_name": relation_name},
        ).mappings().all()
    ]


def _normalized_expression(value: object) -> str | None:
    if value is None:
        return None
    return "".join(str(value).lower().split()).replace("::uuid", "::uuid")


def _require_rls_contract(bind, *, forward: bool) -> None:
    for relation_name in (_ADDRESS_TABLE, _HISTORY_TABLE, _AUDIT_TABLE, _OUTBOX_TABLE):
        contract = _relation_contract(bind, relation_name)
        if contract["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"unexpected owner for public.{relation_name}: {contract['owner_name']}")
        if not contract["relrowsecurity"] or not contract["relforcerowsecurity"]:
            raise RuntimeError(f"public.{relation_name} must keep ENABLE/FORCE RLS")

    expected_context = "(org_id=nullif(current_setting('app.current_org_id'::text,true),''::text)::uuid)"
    address_policies = {row["policy_name"]: row for row in _policy_rows(bind, _ADDRESS_TABLE)}
    required_address = {
        "tenant_isolation_addr_select": "r",
        "tenant_isolation_addr_insert": "a",
        "tenant_isolation_addr_update": "w",
        "tenant_isolation_addr_delete": "d",
    }
    if set(address_policies) != set(required_address):
        raise RuntimeError("organization_addresses RLS policy inventory drifted")
    for name, command in required_address.items():
        row = address_policies[name]
        if row["command"] != command:
            raise RuntimeError(f"organization_addresses policy command drifted: {name}")
        expression = row["check_expression"] if command == "a" else row["using_expression"]
        if _normalized_expression(expression) != expected_context:
            raise RuntimeError(f"organization_addresses tenant predicate drifted: {name}")

    audit = {row["policy_name"]: row for row in _policy_rows(bind, _AUDIT_TABLE)}
    expected_names = {"tenant_isolation_audit_select"}
    if forward:
        expected_names.add(_AUDIT_INSERT_POLICY)
    if set(audit) != expected_names:
        raise RuntimeError("branch_address_audit_log RLS policy inventory drifted")
    select_row = audit["tenant_isolation_audit_select"]
    if select_row["command"] != "r" or _normalized_expression(select_row["using_expression"]) != expected_context:
        raise RuntimeError("audit SELECT tenant policy drifted")
    if forward:
        insert_row = audit[_AUDIT_INSERT_POLICY]
        if insert_row["command"] != "a" or _normalized_expression(insert_row["check_expression"]) != expected_context:
            raise RuntimeError("audit INSERT tenant policy drifted")


def _function_contract(bind, signature: str) -> dict[str, object] | None:
    rows = bind.execute(
        sa.text(
            """
            SELECT
                owner.rolname::text AS owner_name,
                routine.prosecdef,
                routine.proconfig,
                routine.prosrc,
                pg_catalog.has_function_privilege('public', routine.oid, 'EXECUTE') AS public_execute,
                pg_catalog.has_function_privilege(:migration_owner, routine.oid, 'EXECUTE') AS migration_execute
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
            WHERE routine.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature, "migration_owner": _MIGRATION_OWNER},
    ).mappings().all()
    if not rows:
        return None
    return dict(rows[0])


def _require_forward_acl_contract(bind) -> None:
    if set(_direct_privileges(bind, _RUNTIME_ROLE, _ADDRESS_TABLE)) != set(_RUNTIME_PRIVILEGES):
        raise RuntimeError("app_runtime organization_addresses ACL drifted")
    for forbidden in _FORBIDDEN_RUNTIME_PRIVILEGES:
        if _scalar(
            bind,
            "SELECT pg_catalog.has_table_privilege(:role_name, 'public.organization_addresses', :privilege)",
            {"role_name": _RUNTIME_ROLE, "privilege": forbidden},
        ):
            raise RuntimeError(f"app_runtime must not have {forbidden} on organization_addresses")

    for relation_name, expected in _INTERNAL_PRIVILEGES.items():
        if set(_direct_privileges(bind, _SECURITY_OWNER, relation_name)) != set(expected):
            raise RuntimeError(f"app_security_owner internal ACL drifted for {relation_name}")
        if _direct_privileges(bind, _RUNTIME_ROLE, relation_name):
            raise RuntimeError(f"app_runtime must not have direct ACL on internal relation {relation_name}")


def _require_predecessor_acl_contract(bind) -> None:
    if _direct_privileges(bind, _RUNTIME_ROLE, _ADDRESS_TABLE):
        raise RuntimeError("predecessor unexpectedly grants organization_addresses directly to app_runtime")
    for relation_name in _INTERNAL_PRIVILEGES:
        if _direct_privileges(bind, _SECURITY_OWNER, relation_name):
            raise RuntimeError(f"predecessor unexpectedly grants internal DML to app_security_owner: {relation_name}")
    if _function_contract(bind, _HARDENED_INSERT_FUNCTION) is not None or _function_contract(bind, _HARDENED_UPDATE_FUNCTION) is not None:
        raise RuntimeError("hardened address trigger functions already exist in predecessor")


def _set_security_owner(bind) -> None:
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))


def _reset_role(bind) -> None:
    bind.execute(sa.text("RESET ROLE"))


def _create_hardened_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_secure.snapshot_organization_address_on_insert()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                SET row_security = on
                AS $$
                BEGIN
                    IF pg_catalog.current_setting('app.skip_history_snapshot', true) = 'true' THEN
                        RETURN NEW;
                    END IF;
                    INSERT INTO public.branch_address_history
                        (address_id, org_id, dek_version, address_line1, address_line2,
                         city, state_province, country_code, postal_code, formatted_address,
                         valid_from, changed_by)
                    VALUES
                        (NEW.id, NEW.org_id, NEW.dek_version, NEW.address_line1, NEW.address_line2,
                         NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code,
                         NEW.formatted_address, pg_catalog.clock_timestamp(),
                         NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid);
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        bind.execute(
            sa.text(
                """
                CREATE FUNCTION app_secure.snapshot_organization_address_on_change()
                RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                SET row_security = on
                AS $$
                DECLARE
                    v_now timestamptz := pg_catalog.clock_timestamp();
                BEGIN
                    IF NEW._reencryption_in_progress = TRUE THEN
                        IF ROW(OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code)
                           IS NOT DISTINCT FROM
                           ROW(NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN
                            NEW._reencryption_in_progress := FALSE;
                            RETURN NEW;
                        END IF;
                        RAISE EXCEPTION 'plaintext fields mutated during KMS re-encryption pass: address_id=%', OLD.id;
                    END IF;

                    IF ROW(OLD.address_line1, OLD.address_line2, OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code)
                       IS DISTINCT FROM
                       ROW(NEW.address_line1, NEW.address_line2, NEW.city, NEW.state_province, NEW.country_code, NEW.postal_code) THEN
                        UPDATE public.branch_address_history
                        SET valid_to = v_now
                        WHERE address_id = OLD.id AND valid_to IS NULL;

                        INSERT INTO public.branch_address_history
                            (address_id, org_id, dek_version, address_line1, address_line2,
                             city, state_province, country_code, postal_code, formatted_address,
                             valid_from, changed_by)
                        VALUES
                            (OLD.id, OLD.org_id, OLD.dek_version, OLD.address_line1, OLD.address_line2,
                             OLD.city, OLD.state_province, OLD.country_code, OLD.postal_code,
                             OLD.formatted_address, v_now,
                             NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid);

                        INSERT INTO public.branch_address_audit_log
                            (event_id, address_id, org_id, dek_version, old_address, new_address,
                             changed_by, ip_address, user_agent, request_id)
                        VALUES
                            (pg_catalog.gen_random_uuid(), OLD.id, OLD.org_id, OLD.dek_version,
                             pg_catalog.jsonb_build_object(
                                'city', OLD.city, 'state', OLD.state_province,
                                'country_code', OLD.country_code, 'postal_code', OLD.postal_code,
                                'dek_version', OLD.dek_version,
                                'address_line1_hash', pg_catalog.encode(pg_catalog.sha256(OLD.address_line1::bytea), 'hex')
                             ),
                             pg_catalog.jsonb_build_object(
                                'city', NEW.city, 'state', NEW.state_province,
                                'country_code', NEW.country_code, 'postal_code', NEW.postal_code,
                                'dek_version', NEW.dek_version,
                                'address_line1_hash', pg_catalog.encode(pg_catalog.sha256(NEW.address_line1::bytea), 'hex')
                             ),
                             NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid,
                             NULLIF(pg_catalog.current_setting('app.ip_address', true), '')::inet,
                             NULLIF(pg_catalog.current_setting('app.user_agent', true), ''),
                             NULLIF(pg_catalog.current_setting('app.request_id', true), '')::uuid);

                        INSERT INTO public.address_change_outbox
                            (address_id, org_id, event_type, payload)
                        VALUES
                            (NEW.id, NEW.org_id, 'address_updated',
                             pg_catalog.jsonb_build_object('address_id', NEW.id, 'timestamp', v_now));
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        bind.execute(sa.text(f"GRANT USAGE ON SCHEMA app_secure TO {_MIGRATION_OWNER}"))
    finally:
        _reset_role(bind)


def _repoint_triggers(bind, *, hardened: bool) -> None:
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_snapshot_address_on_insert ON public.organization_addresses"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_snapshot_address_history ON public.organization_addresses"))
    if hardened:
        bind.execute(
            sa.text(
                "CREATE TRIGGER trg_snapshot_address_on_insert "
                "AFTER INSERT ON public.organization_addresses FOR EACH ROW "
                "EXECUTE FUNCTION app_secure.snapshot_organization_address_on_insert()"
            )
        )
        bind.execute(
            sa.text(
                "CREATE TRIGGER trg_snapshot_address_history "
                "BEFORE UPDATE ON public.organization_addresses FOR EACH ROW "
                "EXECUTE FUNCTION app_secure.snapshot_organization_address_on_change()"
            )
        )
    else:
        bind.execute(
            sa.text(
                "CREATE TRIGGER trg_snapshot_address_on_insert "
                "AFTER INSERT ON public.organization_addresses FOR EACH ROW "
                "EXECUTE FUNCTION public.snapshot_address_on_insert()"
            )
        )
        bind.execute(
            sa.text(
                "CREATE TRIGGER trg_snapshot_address_history "
                "BEFORE UPDATE ON public.organization_addresses FOR EACH ROW "
                "EXECUTE FUNCTION public.snapshot_address_on_change()"
            )
        )


def _lock_down_hardened_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.snapshot_organization_address_on_insert() FROM PUBLIC"))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.snapshot_organization_address_on_change() FROM PUBLIC"))
        bind.execute(sa.text(f"REVOKE USAGE ON SCHEMA app_secure FROM {_MIGRATION_OWNER}"))
    finally:
        _reset_role(bind)


def _drop_hardened_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(sa.text("DROP FUNCTION app_secure.snapshot_organization_address_on_insert()"))
        bind.execute(sa.text("DROP FUNCTION app_secure.snapshot_organization_address_on_change()"))
    finally:
        _reset_role(bind)


def _require_hardened_function_contract(bind) -> None:
    for signature in (_HARDENED_INSERT_FUNCTION, _HARDENED_UPDATE_FUNCTION):
        row = _function_contract(bind, signature)
        if row is None:
            raise RuntimeError(f"missing hardened trigger function {signature}")
        if row["owner_name"] != _SECURITY_OWNER or not row["prosecdef"]:
            raise RuntimeError(f"hardened trigger function owner/security drifted: {signature}")
        configs = set(row["proconfig"] or [])
        if configs != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"hardened trigger function configuration drifted: {signature}")
        if row["public_execute"] or row["migration_execute"]:
            raise RuntimeError(f"hardened trigger function has an over-broad EXECUTE ACL: {signature}")


def _require_trigger_targets(bind, *, hardened: bool) -> None:
    expected = {
        "trg_snapshot_address_on_insert": (
            "app_secure.snapshot_organization_address_on_insert" if hardened else "public.snapshot_address_on_insert"
        ),
        "trg_snapshot_address_history": (
            "app_secure.snapshot_organization_address_on_change" if hardened else "public.snapshot_address_on_change"
        ),
    }
    rows = bind.execute(
        sa.text(
            """
            SELECT trigger.tgname::text AS trigger_name,
                   namespace.nspname || '.' || routine.proname AS routine_name
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace ON relation_namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_proc AS routine ON routine.oid = trigger.tgfoid
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
            WHERE relation_namespace.nspname = 'public'
              AND relation.relname = 'organization_addresses'
              AND trigger.tgname IN ('trg_snapshot_address_on_insert', 'trg_snapshot_address_history')
              AND NOT trigger.tgisinternal
            """
        )
    ).mappings().all()
    actual = {row["trigger_name"]: row["routine_name"] for row in rows}
    if actual != expected:
        raise RuntimeError(f"organization_addresses trigger target drift: {actual!r}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_rls_contract(bind, forward=False)
    _require_predecessor_acl_contract(bind)
    _require_trigger_targets(bind, hardened=False)

    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.organization_addresses TO app_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.branch_address_history TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE public.branch_address_audit_log TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE public.address_change_outbox TO app_security_owner")
    op.execute(
        "CREATE POLICY tenant_isolation_audit_insert ON public.branch_address_audit_log "
        "FOR INSERT WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)"
    )

    _create_hardened_functions(bind)
    _repoint_triggers(bind, hardened=True)
    _lock_down_hardened_functions(bind)

    _require_rls_contract(bind, forward=True)
    _require_forward_acl_contract(bind)
    _require_hardened_function_contract(bind)
    _require_trigger_targets(bind, hardened=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_rls_contract(bind, forward=True)
    _require_forward_acl_contract(bind)
    _require_hardened_function_contract(bind)
    _require_trigger_targets(bind, hardened=True)

    _repoint_triggers(bind, hardened=False)
    _drop_hardened_functions(bind)
    op.execute("DROP POLICY tenant_isolation_audit_insert ON public.branch_address_audit_log")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE public.organization_addresses FROM app_runtime")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE public.branch_address_history FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE public.branch_address_audit_log FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE public.address_change_outbox FROM app_security_owner")

    _require_rls_contract(bind, forward=False)
    _require_predecessor_acl_contract(bind)
    _require_trigger_targets(bind, hardened=False)
