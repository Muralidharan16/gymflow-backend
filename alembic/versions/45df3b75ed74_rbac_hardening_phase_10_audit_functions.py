"""RBAC Hardening Phase 10 - audit functions

Revision ID: 45df3b75ed74
Revises: f71f231fb001
Create Date: 2026-05-23 16:07:06.330339

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45df3b75ed74'
down_revision: Union[str, Sequence[str], None] = 'f71f231fb001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



# RB1M2V_PHASE10_APP_PRIVATE_OWNER_CONTEXT_START
_RB1M2V_PRIVATE_SCHEMA = "app_private"
_RB1M2V_TARGET_OWNER = "app_security_owner"


def _rb1m2v_bind():
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError(
            f"{revision} requires online catalog access for bounded "
            "app_private owner-context verification."
        )
    bind = op.get_bind()
    if bind is None:
        raise RuntimeError("Alembic online connection is unavailable.")
    return bind


def _rb1m2v_identity(bind):
    return bind.execute(
        sa.text(
            """
            SELECT
                session_user::text AS session_user_name,
                current_user::text AS current_user_name
            """
        )
    ).mappings().one()


def _rb1m2v_require_migration_owner(bind):
    identity = _rb1m2v_identity(bind)
    if (
        identity["session_user_name"] != "migration_owner"
        or identity["current_user_name"] != "migration_owner"
    ):
        raise RuntimeError(
            "Phase-10 owner-context helpers require "
            "session_user=current_user=migration_owner."
        )
    return identity


def _rb1m2v_can_set_security_owner(bind):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.pg_has_role(
                    session_user,
                    'app_security_owner',
                    'SET'
                )
                """
            )
        ).scalar_one()
    )


def _rb1m2v_has_schema_privilege(bind, role_name, privilege):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_schema_privilege(
                    CAST(:role_name AS name),
                    CAST(:schema_name AS name),
                    :privilege
                )
                """
            ),
            {
                "role_name": role_name,
                "schema_name": _RB1M2V_PRIVATE_SCHEMA,
                "privilege": privilege,
            },
        ).scalar_one()
    )


def _rb1m2v_public_schema_privilege(bind, privilege):
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT COALESCE(
                    bool_or(
                        acl.grantee = 0
                        AND acl.privilege_type = :privilege
                    ),
                    FALSE
                )
                FROM pg_catalog.pg_namespace AS namespace
                LEFT JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        namespace.nspacl,
                        pg_catalog.acldefault(
                            'n'::"char",
                            namespace.nspowner
                        )
                    )
                ) AS acl ON TRUE
                WHERE namespace.nspname = :schema_name
                """
            ),
            {
                "privilege": privilege,
                "schema_name": _RB1M2V_PRIVATE_SCHEMA,
            },
        ).scalar_one()
    )


def _rb1m2v_direct_target_acl_rows(bind, privilege):
    rows = bind.execute(
        sa.text(
            """
            SELECT
                grantor_role.rolname::text AS grantor_name,
                grantee_role.rolname::text AS grantee_name,
                acl.privilege_type::text AS privilege_type,
                acl.is_grantable AS is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                namespace.nspacl
            ) AS acl
            JOIN pg_catalog.pg_roles AS grantor_role
              ON grantor_role.oid = acl.grantor
            JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl.grantee
            WHERE namespace.nspname = :schema_name
              AND grantee_role.rolname = :target_owner
              AND acl.privilege_type = :privilege
            ORDER BY
                grantor_role.rolname,
                grantee_role.rolname,
                acl.privilege_type,
                acl.is_grantable
            """
        ),
        {
            "schema_name": _RB1M2V_PRIVATE_SCHEMA,
            "target_owner": _RB1M2V_TARGET_OWNER,
            "privilege": privilege,
        },
    ).mappings().all()
    return tuple(
        (
            row["grantor_name"],
            row["grantee_name"],
            row["privilege_type"],
            bool(row["is_grantable"]),
        )
        for row in rows
    )


def _rb1m2v_preflight_owner_context(bind):
    _rb1m2v_require_migration_owner(bind)
    row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(namespace.nspowner)::text
                    AS schema_owner,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles
                    WHERE rolname = 'app_security_owner'
                ) AS target_exists,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_roles
                    WHERE rolname = 'app_security_owner'
                      AND NOT rolsuper
                      AND NOT rolbypassrls
                      AND NOT rolcanlogin
                      AND NOT rolinherit
                ) AS target_attributes_exact
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = 'app_private'
            """
        )
    ).mappings().first()
    if row is None:
        raise RuntimeError("Required schema app_private is absent.")
    if row["schema_owner"] != "migration_owner":
        raise RuntimeError(
            "app_private must remain owned by migration_owner; "
            f"observed {row['schema_owner']!r}."
        )
    if not row["target_exists"]:
        raise RuntimeError("Required managed role app_security_owner is absent.")
    if not row["target_attributes_exact"]:
        raise RuntimeError(
            "app_security_owner attributes violate the managed-role contract."
        )
    if not _rb1m2v_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    if not _rb1m2v_has_schema_privilege(
        bind, "migration_owner", "CREATE"
    ):
        raise RuntimeError("migration_owner lacks CREATE on app_private.")
    if not _rb1m2v_has_schema_privilege(
        bind, "migration_owner", "USAGE"
    ):
        raise RuntimeError("migration_owner lacks USAGE on app_private.")
    if _rb1m2v_public_schema_privilege(bind, "CREATE"):
        raise RuntimeError("PUBLIC CREATE on app_private is forbidden.")


def _rb1m2v_prepare_app_private_owner_window(bind):
    _rb1m2v_preflight_owner_context(bind)
    before = {}
    effective_before = {}
    added = []
    for privilege in ("USAGE", "CREATE"):
        before[privilege] = _rb1m2v_direct_target_acl_rows(
            bind, privilege
        )
        effective_before[privilege] = _rb1m2v_has_schema_privilege(
            bind, _RB1M2V_TARGET_OWNER, privilege
        )
        if effective_before[privilege]:
            continue

        bind.execute(
            sa.text(
                f"GRANT {privilege} ON SCHEMA app_private "
                "TO app_security_owner"
            )
        )
        after = _rb1m2v_direct_target_acl_rows(bind, privilege)
        delta = [row for row in after if row not in before[privilege]]
        expected = (
            "migration_owner",
            "app_security_owner",
            privilege,
            False,
        )
        if delta != [expected]:
            raise RuntimeError(
                f"Unexpected temporary {privilege} ACL delta: {delta!r}."
            )
        added.append(privilege)
        if not _rb1m2v_has_schema_privilege(
            bind, _RB1M2V_TARGET_OWNER, privilege
        ):
            raise RuntimeError(
                f"app_security_owner lacks effective {privilege} "
                "after bounded preparation."
            )

    if _rb1m2v_public_schema_privilege(bind, "CREATE"):
        raise RuntimeError(
            "PUBLIC CREATE appeared during owner-context preparation."
        )
    return {
        "before": before,
        "effective_before": effective_before,
        "added": tuple(added),
    }


def _rb1m2v_restore_app_private_owner_window(bind, state):
    _rb1m2v_require_migration_owner(bind)
    for privilege in reversed(state["added"]):
        bind.execute(
            sa.text(
                f"REVOKE {privilege} ON SCHEMA app_private "
                "FROM app_security_owner"
            )
        )

    for privilege in ("USAGE", "CREATE"):
        observed = _rb1m2v_direct_target_acl_rows(bind, privilege)
        expected = state["before"][privilege]
        if observed != expected:
            raise RuntimeError(
                f"Exact app_private {privilege} ACL restoration failed: "
                f"observed={observed!r}, expected={expected!r}."
            )
        effective = _rb1m2v_has_schema_privilege(
            bind, _RB1M2V_TARGET_OWNER, privilege
        )
        if effective != state["effective_before"][privilege]:
            raise RuntimeError(
                f"Effective app_private {privilege} restoration failed."
            )

    if _rb1m2v_public_schema_privilege(bind, "CREATE"):
        raise RuntimeError(
            "PUBLIC CREATE on app_private after restoration is forbidden."
        )


def _rb1m2v_function_owner(bind, signature):
    return bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_userbyid(proc.proowner)::text
            FROM pg_catalog.pg_proc AS proc
            WHERE proc.oid = pg_catalog.to_regprocedure(:signature)
            """
        ),
        {"signature": signature},
    ).scalar_one_or_none()


def _rb1m2v_transfer_function_owner(bind, signature):
    _rb1m2v_require_migration_owner(bind)
    if not _rb1m2v_has_schema_privilege(
        bind, _RB1M2V_TARGET_OWNER, "CREATE"
    ):
        raise RuntimeError(
            "app_security_owner lacks CREATE on app_private "
            "required for function owner transfer."
        )
    owner = _rb1m2v_function_owner(bind, signature)
    if owner != "migration_owner":
        raise RuntimeError(
            f"Function {signature} must be owned by migration_owner "
            f"before transfer; observed {owner!r}."
        )
    bind.execute(
        sa.text(
            f"ALTER FUNCTION {signature} "
            "OWNER TO app_security_owner"
        )
    )
    if _rb1m2v_function_owner(bind, signature) != _RB1M2V_TARGET_OWNER:
        raise RuntimeError(
            f"Function {signature} owner transfer did not persist."
        )


def _rb1m2v_run_as_security_owner(bind, statements):
    _rb1m2v_require_migration_owner(bind)
    if not _rb1m2v_can_set_security_owner(bind):
        raise RuntimeError(
            "migration_owner cannot SET ROLE app_security_owner."
        )
    if not _rb1m2v_has_schema_privilege(
        bind, _RB1M2V_TARGET_OWNER, "USAGE"
    ):
        raise RuntimeError(
            "app_security_owner lacks USAGE on app_private."
        )
    statements = (
        statements
        if isinstance(statements, (tuple, list))
        else (statements,)
    )
    bind.execute(sa.text("SET LOCAL ROLE app_security_owner"))
    identity = _rb1m2v_identity(bind)
    if identity["session_user_name"] != "migration_owner":
        raise RuntimeError("SET LOCAL ROLE changed session_user.")
    if identity["current_user_name"] != _RB1M2V_TARGET_OWNER:
        raise RuntimeError(
            "SET LOCAL ROLE did not enter app_security_owner."
        )
    for statement in statements:
        bind.execute(sa.text(statement))
    bind.execute(sa.text("RESET ROLE"))
    _rb1m2v_require_migration_owner(bind)


def _rb1m2v_drop_function_if_exists(bind, signature):
    _rb1m2v_require_migration_owner(bind)
    owner = _rb1m2v_function_owner(bind, signature)
    if owner is None:
        return False
    if owner != _RB1M2V_TARGET_OWNER:
        raise RuntimeError(
            f"Function {signature} has unexpected owner {owner!r}."
        )
    _rb1m2v_run_as_security_owner(
        bind,
        f"DROP FUNCTION {signature} RESTRICT",
    )
    if _rb1m2v_function_owner(bind, signature) is not None:
        raise RuntimeError(
            f"Function {signature} survived owner-context DROP."
        )
    return True
# RB1M2V_PHASE10_APP_PRIVATE_OWNER_CONTEXT_END

def upgrade() -> None:
    bind = _rb1m2v_bind()
    owner_state = _rb1m2v_prepare_app_private_owner_window(bind)

    # 1. Advisory Lock Helper. The predecessor function is owner-managed,
    # so its replacement starts by dropping it under the actual owner role.
    _rb1m2v_drop_function_if_exists(
        bind,
        "app_private.org_advisory_lock_key(uuid)",
    )
    op.execute("""
    CREATE OR REPLACE FUNCTION app_private.org_advisory_lock_key(org_id UUID)
    RETURNS BIGINT STRICT IMMUTABLE PARALLEL SAFE SECURITY DEFINER
    SET search_path = pg_catalog AS $$
    BEGIN
        RETURN (('x' || substr(md5(org_id::text), 1, 16)))::bit(64)::bigint;
    END;
    $$ LANGUAGE plpgsql;
    """)

    _rb1m2v_transfer_function_owner(
        bind,
        "app_private.org_advisory_lock_key(uuid)",
    )
    _rb1m2v_run_as_security_owner(
        bind,
        "REVOKE ALL ON FUNCTION "
        "app_private.org_advisory_lock_key(uuid) FROM PUBLIC",
    )

    # 2. Append Audit Event
    op.execute("""
    CREATE OR REPLACE FUNCTION app_private.append_audit_event(
        p_org_id              UUID,
        p_branch_id           UUID,
        p_actor_id            UUID,
        p_actor_snapshot      JSONB,
        p_actor_permissions   JSONB,
        p_action              VARCHAR(64),
        p_reason_code         VARCHAR(32),
        p_reason              TEXT,
        p_diff                JSONB,
        p_request_id          UUID,
        p_canonical_payload   TEXT,
        p_event_hash          VARCHAR(64)
    )
    RETURNS UUID
    VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog AS $$
    DECLARE
        v_prev_hash VARCHAR(64);
        v_event_id  UUID := gen_random_uuid();
    BEGIN
        -- Serialize hash chain writes per org to prevent fork races
        PERFORM pg_advisory_xact_lock(app_private.org_advisory_lock_key(p_org_id));

        -- Predecessor fetched by sequence only (no timestamp dependency)
        SELECT event_hash INTO v_prev_hash
        FROM public.branch_audit_log
        WHERE org_id = p_org_id
        ORDER BY audit_sequence DESC
        LIMIT 1;

        -- Validate app-supplied hash by re-deriving it inside DB as a sanity check
        IF encode(sha256(convert_to(p_canonical_payload, 'utf8')), 'hex') != p_event_hash THEN
            RAISE EXCEPTION 'Canonical payload hash mismatch — potential tampering detected';
        END IF;

        INSERT INTO public.branch_audit_log (
            event_id, org_id, branch_id, actor_id, actor_snapshot, actor_permissions, action, reason_code, reason,
            diff, request_id, previous_event_hash, event_hash
        ) VALUES (
            v_event_id, p_org_id, p_branch_id, p_actor_id, p_actor_snapshot, p_actor_permissions, p_action, p_reason_code,
            p_reason, p_diff, p_request_id, v_prev_hash, p_event_hash
        );

        RETURN v_event_id;
    END;
    $$ LANGUAGE plpgsql;
    """)

    _rb1m2v_transfer_function_owner(
        bind,
        "app_private.append_audit_event("
        "uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, "
        "text, jsonb, uuid, text, varchar"
        ")",
    )
    _rb1m2v_run_as_security_owner(
        bind,
        (
            "REVOKE ALL ON FUNCTION "
            "app_private.append_audit_event("
            "uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, "
            "text, jsonb, uuid, text, varchar"
            ") FROM PUBLIC",
            "GRANT EXECUTE ON FUNCTION "
            "app_private.append_audit_event("
            "uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, "
            "text, jsonb, uuid, text, varchar"
            ") TO audit_writer",
        ),
    )
    _rb1m2v_restore_app_private_owner_window(bind, owner_state)


def downgrade() -> None:
    bind = _rb1m2v_bind()
    owner_state = _rb1m2v_prepare_app_private_owner_window(bind)

    _rb1m2v_drop_function_if_exists(
        bind,
        "app_private.append_audit_event("
        "uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, "
        "text, jsonb, uuid, text, varchar"
        ")",
    )
    _rb1m2v_drop_function_if_exists(
        bind,
        "app_private.org_advisory_lock_key(uuid)",
    )

    # Restore the exact predecessor (0026/f71-visible) advisory-lock helper.
    # 45df replaces this existing owner-managed function during upgrade, so
    # downgrade must reconstruct the predecessor rather than merely drop the
    # 45df representation.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_private.org_advisory_lock_key(p_org_id UUID)
        RETURNS BIGINT
        STRICT
        IMMUTABLE
        PARALLEL SAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- MD5 hex → first 16 chars → bit(64) → bigint
            -- Deterministic across PG major versions; stable under failover/replication.
            RETURN (('x' || substr(md5(p_org_id::text), 1, 16)))::bit(64)::bigint;
        END;
        $$;
    """)

    _rb1m2v_transfer_function_owner(
        bind,
        "app_private.org_advisory_lock_key(uuid)",
    )
    _rb1m2v_run_as_security_owner(
        bind,
        (
            "REVOKE ALL ON FUNCTION "
            "app_private.org_advisory_lock_key(uuid) FROM PUBLIC",
            "GRANT EXECUTE ON FUNCTION "
            "app_private.org_advisory_lock_key(uuid) TO audit_writer",
        ),
    )
    _rb1m2v_restore_app_private_owner_window(bind, owner_state)
