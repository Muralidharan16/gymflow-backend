"""Enable address RLS and establish least-privilege runtime access.

Revision ID: 6f708192a3b4
Revises: 5e6f708192a3
Create Date: 2026-08-10

The historical address hardening revision created tenant policies and set FORCE
ROW LEVEL SECURITY, but never ENABLEd RLS on the protected relations. This left
the policy catalog present while enforcement remained disabled. It also granted
address writes only to the legacy NOLOGIN ``branch_admin`` role even though the
production address API uses the ordinary application database pool.

Forward contract:
* ENABLE + FORCE RLS on the five address relations that already have tenant
  policies;
* grant app_runtime only SELECT/INSERT/UPDATE on organization_addresses;
* keep history/audit/outbox writes internal through SECURITY DEFINER trigger
  functions owned by app_security_owner, with fixed search_path and row_security;
* add the missing tenant-scoped INSERT policy for the immutable audit ledger;
* grant no DELETE/TRUNCATE/schema-create/ownership/RLS-bypass capability.

Downgrade restores the exact predecessor flag/ACL/trigger state: FORCE remains
set, RLS is disabled, hardened functions/policy/ACLs are removed, and predecessor
triggers are restored.
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa

revision = "6f708192a3b4"
down_revision = "5e6f708192a3"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_RUNTIME_ROLE = "app_runtime"

_ADDRESS = "organization_addresses"
_GEOCODE = "branch_geocode_attempts"
_OUTBOX = "address_change_outbox"
_HISTORY = "branch_address_history"
_AUDIT = "branch_address_audit_log"
_RLS_RELATIONS = (_ADDRESS, _GEOCODE, _OUTBOX, _HISTORY, _AUDIT)

_INSERT_FN = "app_secure.snapshot_organization_address_on_insert()"
_UPDATE_FN = "app_secure.snapshot_organization_address_on_change()"
_AUDIT_INSERT_POLICY = "tenant_isolation_audit_insert"
_RUNTIME_PRIVILEGES = {"SELECT", "INSERT", "UPDATE"}
_FORBIDDEN_RUNTIME = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
_INTERNAL_PRIVILEGES = {
    _HISTORY: {"SELECT", "INSERT", "UPDATE"},
    _AUDIT: {"INSERT"},
    _OUTBOX: {"INSERT"},
}
_TENANT_EXPR = "org_id=nullifcurrent_setting'app.current_org_id'::text,true,''::text::uuid"


def _scalar(bind, sql: str, params: dict[str, object] | None = None):
    return bind.execute(sa.text(sql), params or {}).scalar_one()


def _direct_schema_usage(bind, role_name: str) -> bool:
    return bool(_scalar(bind, """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(ns.nspacl, pg_catalog.acldefault('n', ns.nspowner))
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE ns.nspname = 'app_secure'
              AND grantee.rolname = :role_name
              AND acl.privilege_type = 'USAGE'
        )
    """, {"role_name": role_name}))


def _require_identity(bind) -> None:
    row = bind.execute(sa.text("""
        SELECT session_user::text AS session_name, current_user::text AS current_name,
               r.rolsuper, r.rolinherit, r.rolcreatedb, r.rolcreaterole,
               r.rolreplication, r.rolbypassrls
        FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user
    """)).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("address runtime migration requires migration_owner")
    if any(row[key] for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole",
        "rolreplication", "rolbypassrls",
    )):
        raise RuntimeError("migration_owner violates the reduced role contract")

    roles = bind.execute(sa.text("""
        SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname IN (:security_owner, :runtime_role)
    """), {"security_owner": _SECURITY_OWNER, "runtime_role": _RUNTIME_ROLE}).mappings().all()
    by_name = {item["rolname"]: item for item in roles}
    if set(by_name) != {_SECURITY_OWNER, _RUNTIME_ROLE}:
        raise RuntimeError("required managed address roles are missing")
    for name, role in by_name.items():
        if role["rolcanlogin"] or role["rolsuper"] or role["rolinherit"] or role["rolbypassrls"]:
            raise RuntimeError(f"managed role {name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS")

    edge = bind.execute(sa.text("""
        SELECT m.admin_option, m.inherit_option, m.set_option
        FROM pg_catalog.pg_auth_members AS m
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = m.member
        WHERE granted.rolname = :granted AND member_role.rolname = :member
    """), {"granted": _SECURITY_OWNER, "member": _MIGRATION_OWNER}).mappings().all()
    if len(edge) != 1 or edge[0]["admin_option"] or edge[0]["inherit_option"] or not edge[0]["set_option"]:
        raise RuntimeError("migration_owner -> app_security_owner must be SET-only")

    owner = _scalar(bind, """
        SELECT r.rolname::text
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_roles AS r ON r.oid = ns.nspowner
        WHERE ns.nspname = 'app_secure'
    """)
    if owner != _SECURITY_OWNER:
        raise RuntimeError("app_secure must be owned by app_security_owner")
    if _direct_schema_usage(bind, _MIGRATION_OWNER):
        raise RuntimeError("migration_owner must not retain direct app_secure USAGE")


def _relation(bind, name: str) -> dict[str, object]:
    rows = bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name,
               c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = c.relowner
        WHERE ns.nspname = 'public' AND c.relname = :name AND c.relkind IN ('r','p')
    """), {"name": name}).mappings().all()
    if len(rows) != 1:
        raise RuntimeError(f"missing public.{name}")
    return dict(rows[0])


def _direct_privileges(bind, role_name: str, relation_name: str) -> set[str]:
    return set(bind.execute(sa.text("""
        SELECT DISTINCT acl.privilege_type::text
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))
        ) AS acl
        JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
        WHERE ns.nspname = 'public' AND c.relname = :relation_name
          AND grantee.rolname = :role_name
    """), {"relation_name": relation_name, "role_name": role_name}).scalars().all())


def _policies(bind, relation_name: str) -> dict[str, dict[str, object]]:
    rows = bind.execute(sa.text("""
        SELECT p.polname::text AS name, p.polcmd::text AS command,
               pg_catalog.pg_get_expr(p.polqual, p.polrelid, true)::text AS using_expr,
               pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid, true)::text AS check_expr
        FROM pg_catalog.pg_policy AS p
        JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = 'public' AND c.relname = :relation_name
    """), {"relation_name": relation_name}).mappings().all()
    return {row["name"]: dict(row) for row in rows}


def _tenant_expr(value: object) -> bool:
    if value is None:
        return False
    return re.sub(r"[\s()]", "", str(value).lower()) == _TENANT_EXPR


def _require_policy_contract(bind, *, forward: bool) -> None:
    address = _policies(bind, _ADDRESS)
    expected_address = {
        "tenant_isolation_addr_select": "r",
        "tenant_isolation_addr_insert": "a",
        "tenant_isolation_addr_update": "w",
        "tenant_isolation_addr_delete": "d",
    }
    if set(address) != set(expected_address):
        raise RuntimeError("organization_addresses policy inventory drifted")
    for name, command in expected_address.items():
        row = address[name]
        expr = row["check_expr"] if command == "a" else row["using_expr"]
        if row["command"] != command or not _tenant_expr(expr):
            raise RuntimeError(f"organization_addresses policy drifted: {name}")

    single_policy = {
        _GEOCODE: "geocode_attempts_tenant_isolation",
        _OUTBOX: "outbox_tenant_isolation",
        _HISTORY: "tenant_isolation_hist",
    }
    for relation_name, policy_name in single_policy.items():
        rows = _policies(bind, relation_name)
        if set(rows) != {policy_name}:
            raise RuntimeError(f"{relation_name} policy inventory drifted")
        row = rows[policy_name]
        if row["command"] != "*" or not _tenant_expr(row["using_expr"]):
            raise RuntimeError(f"{relation_name} tenant policy drifted")

    audit = _policies(bind, _AUDIT)
    expected = {"tenant_isolation_audit_select"}
    if forward:
        expected.add(_AUDIT_INSERT_POLICY)
    if set(audit) != expected:
        raise RuntimeError("branch_address_audit_log policy inventory drifted")
    if audit["tenant_isolation_audit_select"]["command"] != "r" or not _tenant_expr(
        audit["tenant_isolation_audit_select"]["using_expr"]
    ):
        raise RuntimeError("audit SELECT policy drifted")
    if forward:
        row = audit[_AUDIT_INSERT_POLICY]
        if row["command"] != "a" or not _tenant_expr(row["check_expr"]):
            raise RuntimeError("audit INSERT policy drifted")


def _require_rls_flags(bind, *, enabled: bool) -> None:
    for name in _RLS_RELATIONS:
        row = _relation(bind, name)
        if row["owner_name"] != _MIGRATION_OWNER:
            raise RuntimeError(f"unexpected owner for public.{name}: {row['owner_name']}")
        if bool(row["relrowsecurity"]) is not enabled:
            raise RuntimeError(f"public.{name} RLS enabled flag drifted")
        if not row["relforcerowsecurity"]:
            raise RuntimeError(f"public.{name} must retain FORCE ROW LEVEL SECURITY")


def _function(bind, signature: str) -> dict[str, object] | None:
    prefix = "app_secure."
    suffix = "()"
    if not signature.startswith(prefix) or not signature.endswith(suffix):
        raise RuntimeError(f"unsupported protected function identity: {signature}")
    function_name = signature[len(prefix):-len(suffix)]
    if not function_name or not function_name.replace("_", "").isalnum():
        raise RuntimeError(f"invalid protected function identity: {signature}")

    rows = bind.execute(sa.text("""
        SELECT owner.rolname::text AS owner_name, p.prosecdef, p.proconfig, p.prosrc,
               EXISTS (
                   SELECT 1 FROM pg_catalog.aclexplode(
                       COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                   ) AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute,
               EXISTS (
                   SELECT 1 FROM pg_catalog.aclexplode(
                       COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                   ) AS acl
                   JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
                   WHERE grantee.rolname = :migration_owner
                     AND acl.privilege_type = 'EXECUTE'
               ) AS migration_direct_execute
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = p.pronamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
        WHERE ns.nspname = 'app_secure'
          AND p.proname = :function_name
          AND p.prokind = 'f'
          AND p.pronargs = 0
    """), {
        "function_name": function_name,
        "migration_owner": _MIGRATION_OWNER,
    }).mappings().all()
    if len(rows) > 1:
        raise RuntimeError(f"ambiguous protected function identity: {signature}")
    return None if not rows else dict(rows[0])


def _trigger_targets(bind) -> dict[str, str]:
    rows = bind.execute(sa.text("""
        SELECT t.tgname::text AS trigger_name,
               fns.nspname || '.' || p.proname AS function_name
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace AS cns ON cns.oid = c.relnamespace
        JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_catalog.pg_namespace AS fns ON fns.oid = p.pronamespace
        WHERE cns.nspname = 'public' AND c.relname = 'organization_addresses'
          AND t.tgname IN ('trg_snapshot_address_on_insert','trg_snapshot_address_history')
          AND NOT t.tgisinternal
    """)).mappings().all()
    return {row["trigger_name"]: row["function_name"] for row in rows}


def _require_trigger_targets(bind, *, hardened: bool) -> None:
    expected = {
        "trg_snapshot_address_on_insert": (
            "app_secure.snapshot_organization_address_on_insert"
            if hardened else "public.snapshot_address_on_insert"
        ),
        "trg_snapshot_address_history": (
            "app_secure.snapshot_organization_address_on_change"
            if hardened else "public.snapshot_address_on_change"
        ),
    }
    actual = _trigger_targets(bind)
    if actual != expected:
        raise RuntimeError(f"address trigger target drift: {actual!r}")


def _require_predecessor(bind) -> None:
    _require_rls_flags(bind, enabled=False)
    _require_policy_contract(bind, forward=False)
    if _direct_privileges(bind, _RUNTIME_ROLE, _ADDRESS):
        raise RuntimeError("predecessor unexpectedly grants organization_addresses to app_runtime")
    for relation_name in _INTERNAL_PRIVILEGES:
        if _direct_privileges(bind, _SECURITY_OWNER, relation_name):
            raise RuntimeError(f"predecessor unexpectedly grants {relation_name} to app_security_owner")
    if _function(bind, _INSERT_FN) is not None or _function(bind, _UPDATE_FN) is not None:
        raise RuntimeError("hardened address functions already exist")
    _require_trigger_targets(bind, hardened=False)


def _require_forward(bind) -> None:
    _require_rls_flags(bind, enabled=True)
    _require_policy_contract(bind, forward=True)
    if _direct_privileges(bind, _RUNTIME_ROLE, _ADDRESS) != _RUNTIME_PRIVILEGES:
        raise RuntimeError("app_runtime organization_addresses ACL drifted")
    for privilege in _FORBIDDEN_RUNTIME:
        if _scalar(bind,
            "SELECT pg_catalog.has_table_privilege(:role_name, 'public.organization_addresses', :privilege)",
            {"role_name": _RUNTIME_ROLE, "privilege": privilege},
        ):
            raise RuntimeError(f"app_runtime must not have {privilege} on organization_addresses")
    for relation_name, expected in _INTERNAL_PRIVILEGES.items():
        if _direct_privileges(bind, _SECURITY_OWNER, relation_name) != expected:
            raise RuntimeError(f"app_security_owner ACL drifted for {relation_name}")
        if _direct_privileges(bind, _RUNTIME_ROLE, relation_name):
            raise RuntimeError(f"app_runtime must not have direct ACL on {relation_name}")
    if _direct_schema_usage(bind, _MIGRATION_OWNER):
        raise RuntimeError("temporary migration_owner app_secure USAGE was not revoked")
    _require_hardened_functions(bind)
    _require_trigger_targets(bind, hardened=True)


def _set_security_owner(bind) -> None:
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))


def _reset_role(bind) -> None:
    bind.execute(sa.text("RESET ROLE"))


def _create_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(sa.text("""
            CREATE FUNCTION app_secure.snapshot_organization_address_on_insert()
            RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog SET row_security = on
            AS $$
            DECLARE v_org uuid := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            BEGIN
                IF v_org IS NULL OR NEW.org_id IS DISTINCT FROM v_org THEN
                    RAISE EXCEPTION 'organization address tenant context mismatch';
                END IF;
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
        """))
        bind.execute(sa.text("""
            CREATE FUNCTION app_secure.snapshot_organization_address_on_change()
            RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog SET row_security = on
            AS $$
            DECLARE
                v_now timestamptz := pg_catalog.clock_timestamp();
                v_org uuid := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            BEGIN
                IF v_org IS NULL OR OLD.org_id IS DISTINCT FROM v_org OR NEW.org_id IS DISTINCT FROM v_org THEN
                    RAISE EXCEPTION 'organization address tenant context mismatch';
                END IF;
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
                             'address_line1_hash', pg_catalog.encode(pg_catalog.sha256(OLD.address_line1::bytea), 'hex')),
                         pg_catalog.jsonb_build_object(
                             'city', NEW.city, 'state', NEW.state_province,
                             'country_code', NEW.country_code, 'postal_code', NEW.postal_code,
                             'dek_version', NEW.dek_version,
                             'address_line1_hash', pg_catalog.encode(pg_catalog.sha256(NEW.address_line1::bytea), 'hex')),
                         NULLIF(pg_catalog.current_setting('app.current_user_id', true), '')::uuid,
                         NULLIF(pg_catalog.current_setting('app.ip_address', true), '')::inet,
                         NULLIF(pg_catalog.current_setting('app.user_agent', true), ''),
                         NULLIF(pg_catalog.current_setting('app.request_id', true), '')::uuid);
                    INSERT INTO public.address_change_outbox(address_id, org_id, event_type, payload)
                    VALUES (NEW.id, NEW.org_id, 'address_updated',
                            pg_catalog.jsonb_build_object('address_id', NEW.id, 'timestamp', v_now));
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        bind.execute(sa.text("GRANT USAGE ON SCHEMA app_secure TO migration_owner"))
    finally:
        _reset_role(bind)


def _repoint_triggers(bind, *, hardened: bool) -> None:
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_snapshot_address_on_insert ON public.organization_addresses"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_snapshot_address_history ON public.organization_addresses"))
    insert_fn = "app_secure.snapshot_organization_address_on_insert()" if hardened else "public.snapshot_address_on_insert()"
    update_fn = "app_secure.snapshot_organization_address_on_change()" if hardened else "public.snapshot_address_on_change()"
    bind.execute(sa.text(
        "CREATE TRIGGER trg_snapshot_address_on_insert AFTER INSERT ON public.organization_addresses "
        f"FOR EACH ROW EXECUTE FUNCTION {insert_fn}"
    ))
    bind.execute(sa.text(
        "CREATE TRIGGER trg_snapshot_address_history BEFORE UPDATE ON public.organization_addresses "
        f"FOR EACH ROW EXECUTE FUNCTION {update_fn}"
    ))


def _lock_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.snapshot_organization_address_on_insert() FROM PUBLIC"))
        bind.execute(sa.text("REVOKE ALL ON FUNCTION app_secure.snapshot_organization_address_on_change() FROM PUBLIC"))
        bind.execute(sa.text("REVOKE USAGE ON SCHEMA app_secure FROM migration_owner"))
    finally:
        _reset_role(bind)


def _drop_functions(bind) -> None:
    _set_security_owner(bind)
    try:
        bind.execute(sa.text("DROP FUNCTION app_secure.snapshot_organization_address_on_insert()"))
        bind.execute(sa.text("DROP FUNCTION app_secure.snapshot_organization_address_on_change()"))
    finally:
        _reset_role(bind)


def _require_hardened_functions(bind) -> None:
    required = {
        _INSERT_FN: ("app.current_org_id", "public.branch_address_history"),
        _UPDATE_FN: (
            "app.current_org_id", "public.branch_address_history",
            "public.branch_address_audit_log", "public.address_change_outbox",
        ),
    }
    for signature, body_tokens in required.items():
        row = _function(bind, signature)
        if row is None or row["owner_name"] != _SECURITY_OWNER or not row["prosecdef"]:
            raise RuntimeError(f"hardened function owner/security drifted: {signature}")
        if set(row["proconfig"] or []) != {"search_path=pg_catalog", "row_security=on"}:
            raise RuntimeError(f"hardened function settings drifted: {signature}")
        if row["public_execute"] or row["migration_direct_execute"]:
            raise RuntimeError(f"hardened function EXECUTE ACL drifted: {signature}")
        if not all(token in (row["prosrc"] or "") for token in body_tokens):
            raise RuntimeError(f"hardened function body drifted: {signature}")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_predecessor(bind)

    for relation_name in _RLS_RELATIONS:
        op.execute(f"ALTER TABLE public.{relation_name} ENABLE ROW LEVEL SECURITY")

    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.organization_addresses TO app_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.branch_address_history TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE public.branch_address_audit_log TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE public.address_change_outbox TO app_security_owner")
    op.execute(
        "CREATE POLICY tenant_isolation_audit_insert ON public.branch_address_audit_log "
        "FOR INSERT WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID)"
    )
    _create_functions(bind)
    _repoint_triggers(bind, hardened=True)
    _lock_functions(bind)
    _require_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity(bind)
    _require_forward(bind)

    _repoint_triggers(bind, hardened=False)
    _drop_functions(bind)
    op.execute("DROP POLICY tenant_isolation_audit_insert ON public.branch_address_audit_log")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE public.organization_addresses FROM app_runtime")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE public.branch_address_history FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE public.branch_address_audit_log FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE public.address_change_outbox FROM app_security_owner")

    for relation_name in reversed(_RLS_RELATIONS):
        op.execute(f"ALTER TABLE public.{relation_name} DISABLE ROW LEVEL SECURITY")

    _require_predecessor(bind)
