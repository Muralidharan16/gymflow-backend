"""Harden branch-hours outbox production and worker boundaries.

Revision ID: e7f8091a2b3c
Revises: d6e7f8091a2b
Create Date: 2026-08-11

The predecessor branch-hours implementation used ORM callbacks to insert into a
legacy global outbox.  The table had no tenant identity or RLS, the API runtime
had no legitimate queue capability, the poller did not atomically claim rows,
and the dedupe key collapsed every change for a branch within the same minute.
The projection worker also reused the API database pool even though projection
writes are intentionally internal-only.

This revision establishes a bounded durable contract:

* ``transactional_outbox`` becomes a branch-hours-only tenant-aware FORCE-RLS
  queue with explicit branch/org event shapes and durable correlation IDs;
* ordinary ``app_runtime`` receives no queue table ACL and may enqueue only
  through fixed SECURITY DEFINER functions owned by ``app_security_owner``;
* ``worker_runtime`` is a dedicated NOLOGIN/NOBYPASSRLS privilege group with
  queue SELECT/INSERT/UPDATE only, source read capability, and projection
  SELECT/INSERT/UPDATE only;
* tenant-scoped worker policies require explicit internal-maintenance context
  on branch/hour/projection tables, while queue claim policies are deliberately
  cross-tenant but restricted to the dedicated worker role;
* no DELETE/TRUNCATE/DDL/BYPASSRLS capability is introduced; and
* populated predecessor rows are migrated only when they are provably the
  legacy ``branch_hours.changed`` shape. Unknown legacy events fail closed.

Downgrade converts representable branch events back to the legacy shape, but
refuses downgrade while organization-level events exist because the predecessor
cannot represent them without semantic loss.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e7f8091a2b3c"
down_revision = "d6e7f8091a2b"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_RUNTIME = "app_runtime"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_MAINTENANCE_TOKEN = "branch_hours_projection"
_BRANCH_ENQUEUE = "public.enqueue_branch_hours_rebuild(uuid,uuid)"
_ORG_ENQUEUE = "public.enqueue_organization_hours_rebuild(uuid)"

_QUEUE = "public.transactional_outbox"
_WORKER_SOURCE_PRIVILEGES = {
    "public.org_branches": {"SELECT"},
    "public.org_branch_state": {"SELECT"},
    "public.organization_operating_hours": {"SELECT"},
    "public.branch_operating_hours": {"SELECT"},
    "public.branch_special_hours": {"SELECT"},
    "public.branch_hours_projection": {"SELECT", "INSERT", "UPDATE"},
    _QUEUE: {"SELECT", "INSERT", "UPDATE"},
}
_FORBIDDEN = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}

_WORKER_POLICIES = {
    _QUEUE: {
        "branch_hours_worker_outbox_select",
        "branch_hours_worker_outbox_insert",
        "branch_hours_worker_outbox_update",
        "branch_hours_internal_outbox_insert",
    },
    "public.org_branches": {"branch_hours_worker_branch_read"},
    "public.org_branch_state": {"branch_hours_worker_branch_state_read"},
    "public.organization_operating_hours": {"branch_hours_worker_org_hours_read"},
    "public.branch_operating_hours": {"branch_hours_worker_branch_hours_read"},
    "public.branch_special_hours": {"branch_hours_worker_special_hours_read"},
    "public.branch_hours_projection": {
        "branch_hours_worker_projection_read",
        "branch_hours_worker_projection_insert",
        "branch_hours_worker_projection_update",
    },
}


def _policy_names(bind, relation: str) -> set[str]:
    return set(
        bind.execute(
            sa.text(
                """
                SELECT policy_data.polname::text
                FROM pg_catalog.pg_policy AS policy_data
                WHERE policy_data.polrelid = CAST(:relation AS regclass)
                """
            ),
            {"relation": relation},
        ).scalars().all()
    )


def _direct_privileges(bind, role_name: str, relation: str) -> set[str]:
    schema_name, relation_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation_data.relacl,
                        pg_catalog.acldefault('r', relation_data.relowner)
                    )
                ) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl_data.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :relation_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "relation_name": relation_name,
                "role_name": role_name,
            },
        ).scalars().all()
    )


def _function_contract(bind, signature: str):
    return bind.execute(
        sa.text(
            """
            SELECT
                procedure_data.oid IS NOT NULL AS function_exists,
                owner_role.rolname::text AS owner_name,
                procedure_data.prosecdef AS security_definer,
                procedure_data.provolatile::text AS volatility,
                procedure_data.proconfig,
                pg_catalog.has_function_privilege(
                    'app_runtime', procedure_data.oid, 'EXECUTE'
                ) AS runtime_execute,
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
            FROM (SELECT pg_catalog.to_regprocedure(:signature) AS oid) AS requested
            LEFT JOIN pg_catalog.pg_proc AS procedure_data
              ON procedure_data.oid = requested.oid
            LEFT JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = procedure_data.proowner
            """
        ),
        {"signature": signature},
    ).mappings().one()


def _require_preflight(bind) -> None:
    identity = bind.execute(
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
        identity["session_name"] != _MIGRATION_OWNER
        or identity["current_name"] != _MIGRATION_OWNER
    ):
        raise RuntimeError("e7f809 branch-hours worker migration requires migration_owner")
    if any(
        bool(identity[key])
        for key in (
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError("migration_owner violates the reduced role contract")

    role_rows = bind.execute(
        sa.text(
            """
            SELECT
                role_data.rolname::text AS role_name,
                role_data.rolcanlogin,
                role_data.rolsuper,
                role_data.rolinherit,
                role_data.rolcreatedb,
                role_data.rolcreaterole,
                role_data.rolreplication,
                role_data.rolbypassrls
            FROM pg_catalog.pg_roles AS role_data
            WHERE role_data.rolname = ANY(CAST(:roles AS text[]))
            """
        ),
        {"roles": [_RUNTIME, _SECURITY_OWNER, _WORKER]},
    ).mappings().all()
    by_name = {row["role_name"]: row for row in role_rows}
    if set(by_name) != {_RUNTIME, _SECURITY_OWNER, _WORKER}:
        raise RuntimeError("e7f809 required runtime/security/worker roles are missing")
    for role_name, row in by_name.items():
        if any(
            bool(row[key])
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolinherit",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise RuntimeError(
                f"managed role {role_name} violates NOLOGIN/NOINHERIT/NOBYPASSRLS"
            )
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.pg_has_role(:member, :role, 'MEMBER')"
        ),
        {"member": _MIGRATION_OWNER, "role": _WORKER},
    ).scalar_one():
        raise RuntimeError("migration_owner must not be a worker_runtime member")

    required_relations = tuple(_WORKER_SOURCE_PRIVILEGES) + ("public.organizations",)
    missing = bind.execute(
        sa.text(
            """
            SELECT relation_name
            FROM unnest(CAST(:relations AS text[])) AS required(relation_name)
            WHERE pg_catalog.to_regclass(required.relation_name) IS NULL
            ORDER BY relation_name
            """
        ),
        {"relations": list(required_relations)},
    ).scalars().all()
    if missing:
        raise RuntimeError(f"e7f809 required relations are missing: {tuple(missing)!r}")

    queue_row = bind.execute(
        sa.text(
            """
            SELECT
                pg_catalog.pg_get_userbyid(relowner)::text AS owner_name,
                relrowsecurity,
                relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _QUEUE},
    ).mappings().one()
    if queue_row["owner_name"] != _MIGRATION_OWNER:
        raise RuntimeError("transactional_outbox owner drifted before e7f809")
    if queue_row["relrowsecurity"] or queue_row["relforcerowsecurity"]:
        raise RuntimeError("e7f809 refuses to adopt pre-existing transactional_outbox RLS")

    target_columns = {
        "tenant_id",
        "branch_id",
        "event_version",
        "correlation_id",
        "available_at",
        "parent_event_id",
    }
    existing_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text
                FROM pg_catalog.pg_attribute AS attribute_data
                WHERE attribute_data.attrelid = CAST(:relation AS regclass)
                  AND attribute_data.attnum > 0
                  AND NOT attribute_data.attisdropped
                  AND attribute_data.attname = ANY(CAST(:columns AS text[]))
                """
            ),
            {"relation": _QUEUE, "columns": sorted(target_columns)},
        ).scalars().all()
    )
    if existing_columns:
        raise RuntimeError(
            f"e7f809 refuses pre-existing queue hardening columns: {sorted(existing_columns)!r}"
        )

    if _policy_names(bind, _QUEUE):
        raise RuntimeError("e7f809 predecessor transactional_outbox unexpectedly has policies")
    for relation, names in _WORKER_POLICIES.items():
        collisions = _policy_names(bind, relation) & names
        if collisions:
            raise RuntimeError(
                f"e7f809 worker policy collision on {relation}: {sorted(collisions)!r}"
            )

    for role_name in (_RUNTIME, _WORKER):
        queue_privileges = _direct_privileges(bind, role_name, _QUEUE)
        if queue_privileges:
            raise RuntimeError(
                f"e7f809 refuses predecessor queue ACL for {role_name}: {sorted(queue_privileges)!r}"
            )
    for relation in _WORKER_SOURCE_PRIVILEGES:
        if relation == _QUEUE:
            continue
        existing = _direct_privileges(bind, _WORKER, relation)
        if existing:
            raise RuntimeError(
                f"e7f809 refuses predecessor worker ACL on {relation}: {sorted(existing)!r}"
            )

    for signature in (_BRANCH_ENQUEUE, _ORG_ENQUEUE):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"),
            {"signature": signature},
        ).scalar_one():
            raise RuntimeError(f"e7f809 enqueue function already exists: {signature}")

    unknown_event_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.transactional_outbox
            WHERE event_type IS DISTINCT FROM 'branch_hours.changed'
            """
        )
    ).scalar_one()
    if unknown_event_count:
        raise RuntimeError(
            "e7f809 cannot safely classify populated legacy outbox rows with "
            f"non-branch-hours event types: count={unknown_event_count}"
        )

    invalid_branch_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.transactional_outbox AS outbox_data
            WHERE outbox_data.event_type = 'branch_hours.changed'
              AND (
                    NOT (outbox_data.payload ? 'branch_id')
                    OR NOT pg_catalog.pg_input_is_valid(
                        outbox_data.payload ->> 'branch_id', 'uuid'
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.id = CAST(
                            outbox_data.payload ->> 'branch_id' AS uuid
                        )
                    )
              )
            """
        )
    ).scalar_one()
    if invalid_branch_count:
        raise RuntimeError(
            "e7f809 legacy branch-hours outbox rows contain invalid branch identity: "
            f"count={invalid_branch_count}"
        )


def _current_org_expr() -> str:
    return """
        CASE
            WHEN pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            )
            THEN CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid
            )
            ELSE CAST(NULL AS uuid)
        END
    """


def _maintenance_expr() -> str:
    return (
        "NULLIF(pg_catalog.current_setting('app.internal_maintenance', true), '') "
        f"= '{_MAINTENANCE_TOKEN}'"
    )


def _harden_queue_shape() -> None:
    op.execute(
        """
        ALTER TABLE public.transactional_outbox
            ADD COLUMN tenant_id uuid,
            ADD COLUMN branch_id uuid,
            ADD COLUMN event_version smallint NOT NULL DEFAULT 1,
            ADD COLUMN correlation_id uuid,
            ADD COLUMN available_at timestamptz,
            ADD COLUMN parent_event_id uuid
        """
    )
    op.execute(
        """
        UPDATE public.transactional_outbox AS outbox_data
        SET tenant_id = branch_data.org_id,
            branch_id = branch_data.id,
            correlation_id = outbox_data.id,
            available_at = outbox_data.created_at,
            event_type = 'branch_hours.branch_changed',
            dedupe_key = 'legacy:' || outbox_data.id::text
        FROM public.org_branches AS branch_data
        WHERE outbox_data.event_type = 'branch_hours.changed'
          AND branch_data.id = CAST(outbox_data.payload ->> 'branch_id' AS uuid)
        """
    )
    op.execute(
        """
        ALTER TABLE public.transactional_outbox
            ALTER COLUMN tenant_id SET NOT NULL,
            ALTER COLUMN correlation_id SET NOT NULL,
            ALTER COLUMN available_at SET NOT NULL,
            ADD CONSTRAINT fk_transactional_outbox_tenant
                FOREIGN KEY (tenant_id) REFERENCES public.organizations(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_transactional_outbox_branch
                FOREIGN KEY (branch_id) REFERENCES public.org_branches(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_transactional_outbox_parent
                FOREIGN KEY (parent_event_id) REFERENCES public.transactional_outbox(id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT chk_transactional_outbox_event_version
                CHECK (event_version = 1),
            ADD CONSTRAINT chk_transactional_outbox_attempts_nonnegative
                CHECK (delivery_attempts >= 0),
            ADD CONSTRAINT chk_transactional_outbox_branch_hours_shape
                CHECK (
                    (
                        event_type = 'branch_hours.branch_changed'
                        AND branch_id IS NOT NULL
                    )
                    OR
                    (
                        event_type = 'branch_hours.organization_changed'
                        AND branch_id IS NULL
                    )
                )
        """
    )
    op.execute("DROP INDEX public.ix_outbox_unprocessed")
    op.execute(
        """
        CREATE INDEX ix_outbox_ready_claim
        ON public.transactional_outbox (available_at, created_at)
        WHERE processed_at IS NULL AND dead_lettered_at IS NULL
        """
    )
    op.execute("ALTER TABLE public.transactional_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.transactional_outbox FORCE ROW LEVEL SECURITY")


def _grant_worker_acl_and_policies() -> None:
    for relation, privileges in _WORKER_SOURCE_PRIVILEGES.items():
        op.execute(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE {relation} TO {_WORKER}"
        )

    current_org = _current_org_expr()
    maintenance = _maintenance_expr()

    op.execute(
        """
        CREATE POLICY branch_hours_worker_outbox_select
        ON public.transactional_outbox
        FOR SELECT TO worker_runtime
        USING (TRUE)
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_worker_outbox_update
        ON public.transactional_outbox
        FOR UPDATE TO worker_runtime
        USING (TRUE)
        WITH CHECK (TRUE)
        """
    )
    op.execute(
        """
        CREATE POLICY branch_hours_worker_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO worker_runtime
        WITH CHECK (
            event_type = 'branch_hours.branch_changed'
            AND branch_id IS NOT NULL
            AND parent_event_id IS NOT NULL
            AND event_version = 1
        )
        """
    )

    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_read
        ON public.org_branches
        FOR SELECT TO worker_runtime
        USING (org_id = {current_org} AND {maintenance})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_state_read
        ON public.org_branch_state
        FOR SELECT TO worker_runtime
        USING (org_id = {current_org} AND {maintenance})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_org_hours_read
        ON public.organization_operating_hours
        FOR SELECT TO worker_runtime
        USING (org_id = {current_org} AND {maintenance})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_branch_hours_read
        ON public.branch_operating_hours
        FOR SELECT TO worker_runtime
        USING (
            {maintenance}
            AND EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = branch_operating_hours.branch_id
                  AND branch_data.org_id = {current_org}
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_special_hours_read
        ON public.branch_special_hours
        FOR SELECT TO worker_runtime
        USING (
            {maintenance}
            AND EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = branch_special_hours.branch_id
                  AND branch_data.org_id = {current_org}
            )
        )
        """
    )
    projection_scope = f"""
        {maintenance}
        AND EXISTS (
            SELECT 1
            FROM public.org_branches AS branch_data
            WHERE branch_data.id = branch_hours_projection.branch_id
              AND branch_data.org_id = {current_org}
        )
    """
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_read
        ON public.branch_hours_projection
        FOR SELECT TO worker_runtime
        USING ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_insert
        ON public.branch_hours_projection
        FOR INSERT TO worker_runtime
        WITH CHECK ({projection_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY branch_hours_worker_projection_update
        ON public.branch_hours_projection
        FOR UPDATE TO worker_runtime
        USING ({projection_scope})
        WITH CHECK ({projection_scope})
        """
    )


def _create_enqueue_boundary() -> None:
    op.execute(
        """
        GRANT INSERT (
            id,
            tenant_id,
            branch_id,
            event_type,
            payload,
            dedupe_key,
            event_version,
            correlation_id,
            available_at,
            parent_event_id
        )
        ON TABLE public.transactional_outbox
        TO app_security_owner
        """
    )

    current_org = _current_org_expr()
    op.execute(
        f"""
        CREATE POLICY branch_hours_internal_outbox_insert
        ON public.transactional_outbox
        FOR INSERT TO app_security_owner
        WITH CHECK (
            tenant_id = {current_org}
            AND parent_event_id IS NULL
            AND event_version = 1
            AND correlation_id IS NOT NULL
            AND (
                (
                    event_type = 'branch_hours.branch_changed'
                    AND branch_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM public.org_branches AS branch_data
                        WHERE branch_data.id = transactional_outbox.branch_id
                          AND branch_data.org_id = transactional_outbox.tenant_id
                    )
                )
                OR
                (
                    event_type = 'branch_hours.organization_changed'
                    AND branch_id IS NULL
                )
            )
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.enqueue_branch_hours_rebuild(
            p_branch_id uuid,
            p_correlation_id uuid
        )
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_org_id uuid;
            v_event_id uuid;
        BEGIN
            IF p_branch_id IS NULL OR p_correlation_id IS NULL THEN
                RAISE EXCEPTION 'branch-hours enqueue requires branch and correlation identifiers'
                    USING ERRCODE = '22023';
            END IF;

            IF NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            ) THEN
                RAISE EXCEPTION 'branch-hours enqueue requires tenant context'
                    USING ERRCODE = '42501';
            END IF;
            v_org_id := CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid
            );

            IF NOT EXISTS (
                SELECT 1
                FROM public.org_branches AS branch_data
                WHERE branch_data.id = p_branch_id
                  AND branch_data.org_id = v_org_id
            ) THEN
                RAISE EXCEPTION 'branch-hours enqueue branch/tenant mismatch'
                    USING ERRCODE = '42501';
            END IF;

            v_event_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.transactional_outbox (
                id,
                tenant_id,
                branch_id,
                event_type,
                payload,
                dedupe_key,
                event_version,
                correlation_id,
                available_at,
                parent_event_id
            )
            VALUES (
                v_event_id,
                v_org_id,
                p_branch_id,
                'branch_hours.branch_changed',
                pg_catalog.jsonb_build_object(
                    'branch_id', p_branch_id,
                    'correlation_id', p_correlation_id
                ),
                v_event_id::text,
                1,
                p_correlation_id,
                pg_catalog.clock_timestamp(),
                NULL
            );
            RETURN v_event_id;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.enqueue_organization_hours_rebuild(
            p_correlation_id uuid
        )
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_org_id uuid;
            v_event_id uuid;
        BEGIN
            IF p_correlation_id IS NULL THEN
                RAISE EXCEPTION 'organization-hours enqueue requires correlation identifier'
                    USING ERRCODE = '22023';
            END IF;
            IF NOT pg_catalog.pg_input_is_valid(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), ''),
                'uuid'
            ) THEN
                RAISE EXCEPTION 'organization-hours enqueue requires tenant context'
                    USING ERRCODE = '42501';
            END IF;
            v_org_id := CAST(
                NULLIF(pg_catalog.current_setting('app.current_org_id', true), '') AS uuid
            );

            v_event_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.transactional_outbox (
                id,
                tenant_id,
                branch_id,
                event_type,
                payload,
                dedupe_key,
                event_version,
                correlation_id,
                available_at,
                parent_event_id
            )
            VALUES (
                v_event_id,
                v_org_id,
                NULL,
                'branch_hours.organization_changed',
                pg_catalog.jsonb_build_object(
                    'org_id', v_org_id,
                    'correlation_id', p_correlation_id
                ),
                v_event_id::text,
                1,
                p_correlation_id,
                pg_catalog.clock_timestamp(),
                NULL
            );
            RETURN v_event_id;
        END;
        $function$;
        """
    )

    # Function creation grants PUBLIC EXECUTE by default. Remove it before and
    # after ownership transfer, then grant only app_runtime while acting as the
    # actual no-login function owner.
    for signature in (
        "public.enqueue_branch_hours_rebuild(uuid,uuid)",
        "public.enqueue_organization_hours_rebuild(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

    op.execute("GRANT CREATE ON SCHEMA public TO app_security_owner")
    op.execute(
        "ALTER FUNCTION public.enqueue_branch_hours_rebuild(uuid,uuid) OWNER TO app_security_owner"
    )
    op.execute(
        "ALTER FUNCTION public.enqueue_organization_hours_rebuild(uuid) OWNER TO app_security_owner"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM app_security_owner")

    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in (
        "public.enqueue_branch_hours_rebuild(uuid,uuid)",
        "public.enqueue_organization_hours_rebuild(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO app_runtime")
    op.execute("RESET ROLE")


def _verify_forward(bind) -> None:
    queue_row = bind.execute(
        sa.text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_catalog.pg_class
            WHERE oid = CAST(:relation AS regclass)
            """
        ),
        {"relation": _QUEUE},
    ).mappings().one()
    if not queue_row["relrowsecurity"] or not queue_row["relforcerowsecurity"]:
        raise RuntimeError("e7f809 transactional_outbox must retain ENABLE + FORCE RLS")

    if _direct_privileges(bind, _RUNTIME, _QUEUE):
        raise RuntimeError("e7f809 leaked direct transactional_outbox ACL to app_runtime")

    for relation, expected in _WORKER_SOURCE_PRIVILEGES.items():
        observed = _direct_privileges(bind, _WORKER, relation)
        if observed != expected:
            raise RuntimeError(
                f"e7f809 worker ACL drift on {relation}: "
                f"observed={sorted(observed)!r}, expected={sorted(expected)!r}"
            )
        if observed & _FORBIDDEN:
            raise RuntimeError(f"e7f809 forbidden worker privilege leaked on {relation}")

    for relation, required_names in _WORKER_POLICIES.items():
        observed = _policy_names(bind, relation)
        if not required_names.issubset(observed):
            raise RuntimeError(
                f"e7f809 worker policy drift on {relation}: "
                f"missing={sorted(required_names - observed)!r}"
            )

    for signature in (_BRANCH_ENQUEUE, _ORG_ENQUEUE):
        function = _function_contract(bind, signature)
        settings = set(function["proconfig"] or [])
        if (
            not function["function_exists"]
            or function["owner_name"] != _SECURITY_OWNER
            or not function["security_definer"]
            or function["volatility"] != "v"
            or not function["runtime_execute"]
            or function["public_execute"]
            or "search_path=pg_catalog, public" not in settings
            or "row_security=on" not in settings
        ):
            raise RuntimeError(
                f"e7f809 enqueue function contract drifted: {signature}: {dict(function)!r}"
            )

    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_schema_privilege('app_security_owner', 'public', 'CREATE')"
        )
    ).scalar_one():
        raise RuntimeError("e7f809 left app_security_owner with public CREATE")

    invalid_rows = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.transactional_outbox AS outbox_data
            WHERE outbox_data.tenant_id IS NULL
               OR outbox_data.correlation_id IS NULL
               OR outbox_data.available_at IS NULL
               OR outbox_data.event_version <> 1
               OR outbox_data.event_type NOT IN (
                    'branch_hours.branch_changed',
                    'branch_hours.organization_changed'
               )
               OR (
                    outbox_data.event_type = 'branch_hours.branch_changed'
                    AND (
                        outbox_data.branch_id IS NULL
                        OR NOT EXISTS (
                            SELECT 1
                            FROM public.org_branches AS branch_data
                            WHERE branch_data.id = outbox_data.branch_id
                              AND branch_data.org_id = outbox_data.tenant_id
                        )
                    )
               )
               OR (
                    outbox_data.event_type = 'branch_hours.organization_changed'
                    AND outbox_data.branch_id IS NOT NULL
               )
            """
        )
    ).scalar_one()
    if invalid_rows:
        raise RuntimeError(f"e7f809 queue data contract drifted: invalid_rows={invalid_rows}")


def _drop_enqueue_boundary() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("DROP FUNCTION public.enqueue_branch_hours_rebuild(uuid,uuid)")
    op.execute("DROP FUNCTION public.enqueue_organization_hours_rebuild(uuid)")
    op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY branch_hours_internal_outbox_insert ON public.transactional_outbox"
    )
    op.execute(
        """
        REVOKE INSERT (
            id,
            tenant_id,
            branch_id,
            event_type,
            payload,
            dedupe_key,
            event_version,
            correlation_id,
            available_at,
            parent_event_id
        )
        ON TABLE public.transactional_outbox
        FROM app_security_owner
        """
    )


def _drop_worker_acl_and_policies() -> None:
    for relation, names in _WORKER_POLICIES.items():
        for name in sorted(names):
            if name == "branch_hours_internal_outbox_insert":
                continue
            op.execute(f"DROP POLICY {name} ON {relation}")

    for relation, privileges in _WORKER_SOURCE_PRIVILEGES.items():
        op.execute(
            f"REVOKE {', '.join(sorted(privileges))} ON TABLE {relation} FROM {_WORKER}"
        )


def _restore_legacy_queue_shape(bind) -> None:
    org_event_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM public.transactional_outbox
            WHERE event_type = 'branch_hours.organization_changed'
            """
        )
    ).scalar_one()
    if org_event_count:
        raise RuntimeError(
            "e7f809 downgrade cannot represent organization-level branch-hours "
            f"events in predecessor schema: count={org_event_count}"
        )

    op.execute(
        """
        UPDATE public.transactional_outbox
        SET event_type = 'branch_hours.changed',
            payload = pg_catalog.jsonb_build_object('branch_id', branch_id),
            dedupe_key = 'downgrade:' || id::text
        WHERE event_type = 'branch_hours.branch_changed'
        """
    )

    op.execute("DROP INDEX public.ix_outbox_ready_claim")
    op.execute("ALTER TABLE public.transactional_outbox NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.transactional_outbox DISABLE ROW LEVEL SECURITY")
    op.execute(
        """
        ALTER TABLE public.transactional_outbox
            DROP CONSTRAINT chk_transactional_outbox_branch_hours_shape,
            DROP CONSTRAINT chk_transactional_outbox_attempts_nonnegative,
            DROP CONSTRAINT chk_transactional_outbox_event_version,
            DROP CONSTRAINT fk_transactional_outbox_parent,
            DROP CONSTRAINT fk_transactional_outbox_branch,
            DROP CONSTRAINT fk_transactional_outbox_tenant,
            DROP COLUMN parent_event_id,
            DROP COLUMN available_at,
            DROP COLUMN correlation_id,
            DROP COLUMN event_version,
            DROP COLUMN branch_id,
            DROP COLUMN tenant_id
        """
    )
    op.execute(
        """
        CREATE INDEX ix_outbox_unprocessed
        ON public.transactional_outbox(created_at)
        WHERE processed_at IS NULL AND dead_lettered_at IS NULL
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    _require_preflight(bind)
    _harden_queue_shape()
    _grant_worker_acl_and_policies()
    _create_enqueue_boundary()
    _verify_forward(bind)


def downgrade() -> None:
    bind = op.get_bind()
    session_user, current_user = bind.execute(
        sa.text("SELECT session_user::text, current_user::text")
    ).one()
    if (session_user, current_user) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("e7f809 downgrade requires migration_owner")
    _verify_forward(bind)
    _drop_enqueue_boundary()
    _drop_worker_acl_and_policies()
    _restore_legacy_queue_shape(bind)
