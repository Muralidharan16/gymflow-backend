"""Add evidence-backed external search delivery and reconciliation boundary.

Revision ID: u07d8e9f0a35
Revises: t07d8e9f0a34
Create Date: 2026-08-16

P4B keeps lifecycle search commands in the certified leased branch outbox, but
moves branch projection reads, provider evidence persistence and reconciliation
enqueue behind app_security_owner SECURITY DEFINER capabilities. Queue payloads
and stale event labels are never search authority: a live search-event lease is
validated and the current PostgreSQL projection/version is derived at claim.

No runtime role receives new direct table CRUD. lifecycle_maintenance_runtime
receives only the bounded reconciliation enqueue function. worker_runtime
receives only projection/evidence functions and retains its predecessor outbox
lease rights unchanged.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "u07d8e9f0a35"
down_revision = "t07d8e9f0a34"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER = "worker_runtime"
_MAINTENANCE = "lifecycle_maintenance_runtime"
_STATE = "public.org_branch_state"
_BRANCH = "public.org_branches"
_OUTBOX = "public.branch_outbox_events"
_ATTEMPTS = "public.branch_search_effect_attempts"

# Direct app_security_owner grants owned by predecessor revisions and required by
# P4B's bounded capabilities.  u07 must preserve these on both upgrade and
# downgrade; it grants/revokes only the P4B-owned deltas below.
_BRANCH_INHERITED_SELECT_COLUMNS = ("id", "org_id")
_STATE_INHERITED_SELECT_COLUMNS = ("branch_id", "deleted_at", "is_active")
_OUTBOX_INHERITED_SELECT_COLUMNS = (
    "outbox_id",
    "tenant_id",
    "branch_id",
    "event_type",
    "status",
    "leased_by",
    "leased_until",
    "correlation_id",
)
_OUTBOX_INHERITED_INSERT_COLUMNS = (
    "outbox_id",
    "tenant_id",
    "branch_id",
    "event_type",
    "payload",
    "created_at",
    "process_after",
    "status",
    "attempt_count",
    "max_attempts",
    "correlation_id",
    "leased_by",
    "leased_until",
)

# P4B-owned direct column grants only.  Keeping these sets disjoint from the
# predecessor grants is what makes downgrade ownership exact.
_STATE_SELECT_COLUMNS = (
    "org_id",
    "status",
    "is_operational",
    "is_public",
    "search_visibility_version",
    "search_provider_ack_version",
    "search_provider_document_hash",
    "search_provider_evidence_sha256",
    "search_provider_code",
    "search_provider_index",
    "search_provider_document_id",
    "search_provider_acknowledged_at",
    "search_provider_reconciled_at",
)
_STATE_UPDATE_COLUMNS = (
    "search_visibility_version",
    "search_last_synced_at",
    "search_sync_failed_at",
    "search_provider_ack_version",
    "search_provider_document_hash",
    "search_provider_evidence_sha256",
    "search_provider_code",
    "search_provider_index",
    "search_provider_document_id",
    "search_provider_acknowledged_at",
    "search_provider_reconciled_at",
)
_BRANCH_SELECT_COLUMNS = (
    "branch_name",
    "internal_slug",
    "timezone",
    "region_code",
    "country_code",
)
_OUTBOX_UPDATE_COLUMNS = ("status", "last_error", "leased_by", "leased_until")

_FUNCTIONS = (
    "app_secure.claim_branch_search_projection(uuid,uuid)",
    "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text)",
    "app_secure.record_branch_search_failure(uuid,uuid,bigint,text,text,text,text,text)",
    "app_secure.enqueue_branch_search_reconciliation(integer)",
)
_TRIGGER_FUNCTIONS = (
    "app_secure.bump_branch_search_version_from_state()",
    "app_secure.bump_branch_search_version_from_branch()",
)


def _require_reduced_role(bind, role_name: str, *, login: bool = False) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = :role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"u07 requires externally provisioned role {role_name}")
    if bool(row["rolcanlogin"]) != login:
        raise RuntimeError(f"u07 role login contract drift: {role_name}")
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
        raise RuntimeError(f"u07 reduced role contract drift: {role_name}")


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,
                   current_user::text AS current_name,
                   rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("u07 P4B migration requires migration_owner")
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
        raise RuntimeError("u07 migration_owner violates reduced role contract")

    for role_name in (_SECURITY_OWNER, _WORKER, _MAINTENANCE):
        _require_reduced_role(bind, role_name)
    for runtime in ("app_runtime", "auth_runtime", _WORKER, _MAINTENANCE):
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": runtime, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"u07 runtime may SET ROLE app_security_owner: {runtime}")


def _direct_column_privileges(bind, relation: str, role_name: str) -> set[tuple[str, str]]:
    schema_name, table_name = relation.split(".", 1)
    return set(
        bind.execute(
            sa.text(
                """
                SELECT attribute_data.attname::text, acl_data.privilege_type::text
                FROM pg_catalog.pg_class AS relation_data
                JOIN pg_catalog.pg_namespace AS namespace_data
                  ON namespace_data.oid = relation_data.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute_data
                  ON attribute_data.attrelid = relation_data.oid
                 AND attribute_data.attnum > 0
                 AND NOT attribute_data.attisdropped
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute_data.attacl) AS acl_data
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = acl_data.grantee
                WHERE namespace_data.nspname = :schema_name
                  AND relation_data.relname = :table_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "role_name": role_name,
            },
        ).all()
    )


def _direct_relation_privileges(bind, relation: str, role_name: str) -> set[str]:
    schema_name, table_name = relation.split(".", 1)
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
                  AND relation_data.relname = :table_name
                  AND grantee.rolname = :role_name
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "role_name": role_name,
            },
        ).scalars().all()
    )


def _require_no_new_runtime_table_acl(bind) -> None:
    for role_name in ("app_runtime", "auth_runtime", _MAINTENANCE):
        if _direct_relation_privileges(bind, _ATTEMPTS, role_name):
            raise RuntimeError(f"u07 leaked direct search-attempt ACL to {role_name}")
    if _direct_relation_privileges(bind, _ATTEMPTS, _WORKER):
        raise RuntimeError("u07 leaked direct search-attempt ACL to worker_runtime")


def _require_absent_direct_grants(
    bind,
    relation: str,
    role_name: str,
    expected: set[tuple[str, str]],
) -> None:
    existing = _direct_column_privileges(bind, relation, role_name)
    overlap = existing.intersection(expected)
    if overlap:
        rendered = ", ".join(f"{column}:{privilege}" for column, privilege in sorted(overlap))
        raise RuntimeError(
            "u07 refuses ambiguous predecessor app_security_owner column ACL "
            f"on {relation}: {rendered}"
        )


def _require_inherited_direct_grants(bind) -> None:
    required = {
        _BRANCH: {(column, "SELECT") for column in _BRANCH_INHERITED_SELECT_COLUMNS},
        _STATE: {(column, "SELECT") for column in _STATE_INHERITED_SELECT_COLUMNS},
        _OUTBOX: (
            {(column, "SELECT") for column in _OUTBOX_INHERITED_SELECT_COLUMNS}
            | {(column, "INSERT") for column in _OUTBOX_INHERITED_INSERT_COLUMNS}
        ),
    }
    for relation, expected in required.items():
        existing = _direct_column_privileges(bind, relation, _SECURITY_OWNER)
        missing = expected.difference(existing)
        if missing:
            rendered = ", ".join(
                f"{column}:{privilege}" for column, privilege in sorted(missing)
            )
            raise RuntimeError(
                "u07 predecessor app_security_owner ACL drift "
                f"on {relation}: missing {rendered}"
            )


def _require_predecessor(bind) -> None:
    for relation in (_STATE, _BRANCH, _OUTBOX):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"u07 missing predecessor relation {relation}")
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"),
        {"relation": _ATTEMPTS},
    ).scalar_one():
        raise RuntimeError("u07 search attempt relation already exists")

    existing_function_names = {
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT p.proname::text
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'app_secure'
                  AND p.proname = ANY(CAST(:names AS text[]))
                """
            ),
            {
                "names": [
                    "claim_branch_search_projection",
                    "acknowledge_branch_search_effect",
                    "record_branch_search_failure",
                    "enqueue_branch_search_reconciliation",
                    "bump_branch_search_version_from_state",
                    "bump_branch_search_version_from_branch",
                ]
            },
        ).all()
    }
    if existing_function_names:
        raise RuntimeError(
            "u07 function collision: " + ", ".join(sorted(existing_function_names))
        )

    existing_columns = set(
        bind.execute(
            sa.text(
                """
                SELECT attname::text
                FROM pg_catalog.pg_attribute
                WHERE attrelid = CAST(:relation AS regclass)
                  AND attnum > 0 AND NOT attisdropped
                  AND attname = ANY(CAST(:columns AS text[]))
                """
            ),
            {
                "relation": _STATE,
                "columns": [
                    "search_provider_ack_version",
                    "search_provider_document_hash",
                    "search_provider_evidence_sha256",
                    "search_provider_code",
                    "search_provider_index",
                    "search_provider_document_id",
                    "search_provider_acknowledged_at",
                    "search_provider_reconciled_at",
                ],
            },
        ).scalars().all()
    )
    if existing_columns:
        raise RuntimeError(f"u07 state column collision: {sorted(existing_columns)!r}")

    _require_inherited_direct_grants(bind)
    _require_absent_direct_grants(
        bind,
        _BRANCH,
        _SECURITY_OWNER,
        {(column, "SELECT") for column in _BRANCH_SELECT_COLUMNS},
    )
    _require_absent_direct_grants(
        bind,
        _STATE,
        _SECURITY_OWNER,
        {(column, "SELECT") for column in _STATE_SELECT_COLUMNS}
        | {(column, "UPDATE") for column in _STATE_UPDATE_COLUMNS},
    )
    _require_absent_direct_grants(
        bind,
        _OUTBOX,
        _SECURITY_OWNER,
        {("attempt_count", "SELECT")}
        | {(column, "UPDATE") for column in _OUTBOX_UPDATE_COLUMNS},
    )


def _create_secure_functions() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        """
        CREATE FUNCTION app_secure.claim_branch_search_projection(
            p_outbox_id uuid,
            p_worker_id uuid
        )
        RETURNS TABLE (
            tenant_id uuid,
            branch_id uuid,
            operation text,
            desired_version bigint,
            document jsonb,
            previous_ack_version bigint
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_tenant uuid;
            v_branch uuid;
        BEGIN
            IF p_outbox_id IS NULL OR p_worker_id IS NULL THEN
                RAISE EXCEPTION 'search projection claim requires outbox and worker ids'
                    USING ERRCODE = '22023';
            END IF;

            SELECT o.tenant_id, o.branch_id
            INTO v_tenant, v_branch
            FROM public.branch_outbox_events AS o
            WHERE o.outbox_id = p_outbox_id
              AND o.event_type IN ('branch.search_index', 'branch.search_deindex')
              AND o.status = 'processing'
              AND o.leased_by = p_worker_id
              AND o.leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search projection requires a live owned search lease'
                    USING ERRCODE = '42501';
            END IF;

            RETURN QUERY
            SELECT
                s.org_id,
                s.branch_id,
                CASE
                    WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                    THEN 'index'::text
                    ELSE 'delete'::text
                END,
                s.search_visibility_version,
                CASE
                    WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                    THEN pg_catalog.jsonb_build_object(
                        'branch_id', b.id::text,
                        'organization_id', b.org_id::text,
                        'name', b.branch_name,
                        'slug', b.internal_slug::text,
                        'timezone', b.timezone,
                        'region_code', b.region_code,
                        'country_code', b.country_code,
                        'status', s.status,
                        'is_operational', s.is_operational,
                        'is_public', s.is_public,
                        'search_version', s.search_visibility_version
                    )
                    ELSE NULL::jsonb
                END,
                s.search_provider_ack_version
            FROM public.org_branch_state AS s
            JOIN public.org_branches AS b
              ON b.id = s.branch_id AND b.org_id = s.org_id
            WHERE s.branch_id = v_branch AND s.org_id = v_tenant;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search projection branch is no longer authoritative'
                    USING ERRCODE = 'P0002';
            END IF;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.acknowledge_branch_search_effect(
            p_outbox_id uuid,
            p_worker_id uuid,
            p_desired_version bigint,
            p_operation text,
            p_provider_code text,
            p_provider_index text,
            p_provider_document_id text,
            p_request_sha256 text,
            p_provider_version bigint,
            p_provider_evidence_sha256 text,
            p_document_sha256 text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_tenant uuid;
            v_branch uuid;
            v_attempt integer;
            v_current_version bigint;
            v_current_operation text;
            v_applied boolean := false;
        BEGIN
            IF p_outbox_id IS NULL OR p_worker_id IS NULL
               OR p_desired_version IS NULL OR p_desired_version < 1
               OR p_operation NOT IN ('index', 'delete')
               OR p_provider_code IS NULL OR btrim(p_provider_code) = ''
               OR p_provider_index IS NULL OR btrim(p_provider_index) = ''
               OR p_provider_document_id IS NULL OR btrim(p_provider_document_id) = ''
               OR p_request_sha256 !~ '^[0-9a-f]{64}$'
               OR p_provider_evidence_sha256 !~ '^[0-9a-f]{64}$'
               OR (p_document_sha256 IS NOT NULL AND p_document_sha256 !~ '^[0-9a-f]{64}$')
               OR (p_operation = 'index' AND p_document_sha256 IS NULL)
               OR (p_operation = 'delete' AND p_document_sha256 IS NOT NULL)
            THEN
                RAISE EXCEPTION 'invalid search acknowledgement arguments'
                    USING ERRCODE = '22023';
            END IF;

            SELECT o.tenant_id, o.branch_id, o.attempt_count
            INTO v_tenant, v_branch, v_attempt
            FROM public.branch_outbox_events AS o
            WHERE o.outbox_id = p_outbox_id
              AND o.event_type IN ('branch.search_index', 'branch.search_deindex')
              AND o.status = 'processing'
              AND o.leased_by = p_worker_id
              AND o.leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search acknowledgement requires a live owned lease'
                    USING ERRCODE = '42501';
            END IF;

            SELECT s.search_visibility_version,
                   CASE
                       WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                       THEN 'index'::text ELSE 'delete'::text
                   END
            INTO v_current_version, v_current_operation
            FROM public.org_branch_state AS s
            WHERE s.branch_id = v_branch AND s.org_id = v_tenant
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search acknowledgement branch state missing'
                    USING ERRCODE = 'P0002';
            END IF;

            INSERT INTO public.branch_search_effect_attempts (
                attempt_id, outbox_id, tenant_id, branch_id, desired_version,
                operation, attempt_number, outcome, provider_code, provider_index,
                provider_document_id, request_sha256, provider_version,
                provider_evidence_sha256, document_sha256, created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), p_outbox_id, v_tenant, v_branch,
                p_desired_version, p_operation, v_attempt, 'definite_success',
                p_provider_code, p_provider_index, p_provider_document_id,
                p_request_sha256, p_provider_version, p_provider_evidence_sha256,
                p_document_sha256, pg_catalog.clock_timestamp()
            ) ON CONFLICT (outbox_id, attempt_number) DO NOTHING;

            IF v_current_version = p_desired_version
               AND v_current_operation = p_operation
            THEN
                UPDATE public.org_branch_state
                SET search_provider_ack_version = p_desired_version,
                    search_provider_document_hash = p_document_sha256,
                    search_provider_evidence_sha256 = p_provider_evidence_sha256,
                    search_provider_code = p_provider_code,
                    search_provider_index = p_provider_index,
                    search_provider_document_id = p_provider_document_id,
                    search_provider_acknowledged_at = pg_catalog.clock_timestamp(),
                    search_provider_reconciled_at = pg_catalog.clock_timestamp(),
                    search_last_synced_at = pg_catalog.clock_timestamp(),
                    search_sync_failed_at = NULL
                WHERE branch_id = v_branch AND org_id = v_tenant;
                v_applied := true;
            END IF;

            UPDATE public.branch_outbox_events
            SET status = CASE WHEN v_applied THEN 'delivered' ELSE 'superseded' END,
                leased_by = NULL,
                leased_until = NULL,
                last_error = NULL
            WHERE outbox_id = p_outbox_id
              AND status = 'processing'
              AND leased_by = p_worker_id
              AND leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search acknowledgement lost its lease fence'
                    USING ERRCODE = '40001';
            END IF;

            RETURN v_applied;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.record_branch_search_failure(
            p_outbox_id uuid,
            p_worker_id uuid,
            p_desired_version bigint,
            p_operation text,
            p_outcome text,
            p_provider_code text,
            p_request_sha256 text,
            p_error_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_tenant uuid;
            v_branch uuid;
            v_attempt integer;
            v_current_version bigint;
            v_current_operation text;
        BEGIN
            IF p_desired_version IS NULL OR p_desired_version < 1
               OR p_operation NOT IN ('index', 'delete')
               OR p_outcome NOT IN (
                    'provider_accepted_nonterminal',
                    'permanent_rejection',
                    'retryable_failure',
                    'ambiguous_outcome'
               )
               OR p_provider_code IS NULL OR btrim(p_provider_code) = ''
               OR p_request_sha256 !~ '^[0-9a-f]{64}$'
               OR p_error_code IS NULL OR btrim(p_error_code) = ''
            THEN
                RAISE EXCEPTION 'invalid search failure arguments'
                    USING ERRCODE = '22023';
            END IF;

            SELECT o.tenant_id, o.branch_id, o.attempt_count
            INTO v_tenant, v_branch, v_attempt
            FROM public.branch_outbox_events AS o
            WHERE o.outbox_id = p_outbox_id
              AND o.event_type IN ('branch.search_index', 'branch.search_deindex')
              AND o.status = 'processing'
              AND o.leased_by = p_worker_id
              AND o.leased_until > pg_catalog.clock_timestamp();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'search failure recording requires a live owned lease'
                    USING ERRCODE = '42501';
            END IF;

            SELECT s.search_visibility_version,
                   CASE
                       WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                       THEN 'index'::text ELSE 'delete'::text
                   END
            INTO v_current_version, v_current_operation
            FROM public.org_branch_state AS s
            WHERE s.branch_id = v_branch AND s.org_id = v_tenant;

            INSERT INTO public.branch_search_effect_attempts (
                attempt_id, outbox_id, tenant_id, branch_id, desired_version,
                operation, attempt_number, outcome, provider_code,
                request_sha256, error_code, created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), p_outbox_id, v_tenant, v_branch,
                p_desired_version, p_operation, v_attempt, p_outcome,
                p_provider_code, p_request_sha256, left(p_error_code, 160),
                pg_catalog.clock_timestamp()
            ) ON CONFLICT (outbox_id, attempt_number) DO NOTHING;

            IF v_current_version IS DISTINCT FROM p_desired_version
               OR v_current_operation IS DISTINCT FROM p_operation
            THEN
                UPDATE public.branch_outbox_events
                SET status = 'superseded', leased_by = NULL, leased_until = NULL,
                    last_error = 'search_projection_superseded'
                WHERE outbox_id = p_outbox_id
                  AND status = 'processing'
                  AND leased_by = p_worker_id;
                RETURN false;
            END IF;

            UPDATE public.org_branch_state
            SET search_sync_failed_at = pg_catalog.clock_timestamp()
            WHERE branch_id = v_branch AND org_id = v_tenant;
            RETURN true;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.enqueue_branch_search_reconciliation(p_batch_size integer)
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        DECLARE
            v_count integer;
        BEGIN
            IF pg_catalog.current_setting('app.internal_maintenance', true)
                   IS DISTINCT FROM 'lifecycle'
            THEN
                RAISE EXCEPTION 'search reconciliation requires lifecycle maintenance context'
                    USING ERRCODE = '42501';
            END IF;
            IF p_batch_size IS NULL OR p_batch_size < 1 OR p_batch_size > 100 THEN
                RAISE EXCEPTION 'search reconciliation batch size must be 1..100'
                    USING ERRCODE = '22023';
            END IF;

            WITH candidates AS (
                SELECT s.branch_id, s.org_id,
                       CASE
                           WHEN s.is_operational AND s.is_public AND s.deleted_at IS NULL
                           THEN 'branch.search_index'::text
                           ELSE 'branch.search_deindex'::text
                       END AS event_type,
                       s.search_visibility_version
                FROM public.org_branch_state AS s
                WHERE (
                        s.search_provider_ack_version IS DISTINCT FROM s.search_visibility_version
                        OR s.search_provider_reconciled_at IS NULL
                        OR s.search_provider_reconciled_at < pg_catalog.clock_timestamp() - INTERVAL '24 hours'
                      )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM public.branch_outbox_events AS existing
                        WHERE existing.branch_id = s.branch_id
                          AND existing.tenant_id = s.org_id
                          AND existing.event_type IN ('branch.search_index','branch.search_deindex')
                          AND existing.status IN ('pending','processing')
                  )
                ORDER BY s.search_provider_reconciled_at NULLS FIRST,
                         s.search_visibility_version,
                         s.branch_id
                LIMIT p_batch_size
                FOR UPDATE SKIP LOCKED
            ), inserted AS (
                INSERT INTO public.branch_outbox_events (
                    outbox_id, tenant_id, branch_id, event_type, payload,
                    created_at, process_after, status, attempt_count,
                    max_attempts, correlation_id, leased_by, leased_until
                )
                SELECT
                    pg_catalog.gen_random_uuid(), c.org_id, c.branch_id, c.event_type,
                    pg_catalog.jsonb_build_object(
                        'source', 'search_reconciliation',
                        'desired_version', c.search_visibility_version
                    ),
                    pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp(),
                    'pending', 0, 5, pg_catalog.gen_random_uuid(), NULL, NULL
                FROM candidates AS c
                RETURNING 1
            )
            SELECT count(*)::integer INTO v_count FROM inserted;
            RETURN v_count;
        END;
        $function$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.bump_branch_search_version_from_state()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            IF ROW(NEW.status, NEW.is_operational, NEW.is_public, NEW.deleted_at)
               IS DISTINCT FROM
               ROW(OLD.status, OLD.is_operational, OLD.is_public, OLD.deleted_at)
            THEN
                NEW.search_visibility_version := OLD.search_visibility_version + 1;
                NEW.search_last_synced_at := NULL;
                NEW.search_sync_failed_at := NULL;
            END IF;
            RETURN NEW;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.bump_branch_search_version_from_branch()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        SET row_security = on
        AS $function$
        BEGIN
            IF ROW(
                NEW.branch_name, NEW.internal_slug, NEW.timezone,
                NEW.region_code, NEW.country_code
            ) IS DISTINCT FROM ROW(
                OLD.branch_name, OLD.internal_slug, OLD.timezone,
                OLD.region_code, OLD.country_code
            )
            THEN
                UPDATE public.org_branch_state
                SET search_visibility_version = search_visibility_version + 1,
                    search_last_synced_at = NULL,
                    search_sync_failed_at = NULL
                WHERE branch_id = NEW.id AND org_id = NEW.org_id;
            END IF;
            RETURN NEW;
        END;
        $function$;
        """
    )

    for signature in (*_FUNCTIONS, *_TRIGGER_FUNCTIONS):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.claim_branch_search_projection(uuid,uuid) "
        "TO worker_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.acknowledge_branch_search_effect(uuid,uuid,bigint,text,text,text,text,text,bigint,text,text) "
        "TO worker_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "app_secure.record_branch_search_failure(uuid,uuid,bigint,text,text,text,text,text) "
        "TO worker_runtime"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.enqueue_branch_search_reconciliation(integer) "
        "TO lifecycle_maintenance_runtime"
    )
    op.execute("RESET ROLE")


def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)

    op.execute(
        """
        ALTER TABLE public.org_branch_state
            ADD COLUMN search_provider_ack_version bigint,
            ADD COLUMN search_provider_document_hash char(64),
            ADD COLUMN search_provider_evidence_sha256 char(64),
            ADD COLUMN search_provider_code text,
            ADD COLUMN search_provider_index text,
            ADD COLUMN search_provider_document_id text,
            ADD COLUMN search_provider_acknowledged_at timestamptz,
            ADD COLUMN search_provider_reconciled_at timestamptz,
            ADD CONSTRAINT chk_branch_search_provider_ack_version
                CHECK (search_provider_ack_version IS NULL OR search_provider_ack_version >= 1),
            ADD CONSTRAINT chk_branch_search_provider_document_hash
                CHECK (search_provider_document_hash IS NULL OR search_provider_document_hash ~ '^[0-9a-f]{64}$'),
            ADD CONSTRAINT chk_branch_search_provider_evidence_hash
                CHECK (search_provider_evidence_sha256 IS NULL OR search_provider_evidence_sha256 ~ '^[0-9a-f]{64}$'),
            ADD CONSTRAINT chk_branch_search_provider_ack_shape
                CHECK (
                    search_provider_ack_version IS NULL
                    OR (
                        search_provider_code IS NOT NULL
                        AND btrim(search_provider_code) <> ''
                        AND search_provider_index IS NOT NULL
                        AND btrim(search_provider_index) <> ''
                        AND search_provider_document_id IS NOT NULL
                        AND btrim(search_provider_document_id) <> ''
                        AND search_provider_evidence_sha256 IS NOT NULL
                        AND search_provider_acknowledged_at IS NOT NULL
                        AND search_provider_reconciled_at IS NOT NULL
                    )
                )
        """
    )
    op.execute(
        """
        CREATE TABLE public.branch_search_effect_attempts (
            attempt_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            outbox_id uuid NOT NULL REFERENCES public.branch_outbox_events(outbox_id) ON DELETE RESTRICT,
            tenant_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            branch_id uuid NOT NULL REFERENCES public.org_branches(id) ON DELETE RESTRICT,
            desired_version bigint NOT NULL CHECK (desired_version >= 1),
            operation text NOT NULL CHECK (operation IN ('index','delete')),
            attempt_number integer NOT NULL CHECK (attempt_number >= 1),
            outcome text NOT NULL CHECK (outcome IN (
                'definite_success','provider_accepted_nonterminal',
                'permanent_rejection','retryable_failure','ambiguous_outcome'
            )),
            provider_code text NOT NULL CHECK (btrim(provider_code) <> ''),
            provider_index text,
            provider_document_id text,
            request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            provider_version bigint,
            provider_evidence_sha256 char(64) CHECK (
                provider_evidence_sha256 IS NULL OR provider_evidence_sha256 ~ '^[0-9a-f]{64}$'
            ),
            document_sha256 char(64) CHECK (
                document_sha256 IS NULL OR document_sha256 ~ '^[0-9a-f]{64}$'
            ),
            error_code text,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_branch_search_attempt_outbox_number UNIQUE (outbox_id, attempt_number)
        )
        """
    )
    op.execute("ALTER TABLE public.branch_search_effect_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.branch_search_effect_attempts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE INDEX ix_branch_search_attempts_branch_created "
        "ON public.branch_search_effect_attempts (branch_id, created_at DESC)"
    )

    op.execute(
        "GRANT SELECT (" + ",".join(_BRANCH_SELECT_COLUMNS) + ") "
        "ON TABLE public.org_branches TO app_security_owner"
    )
    op.execute(
        "GRANT SELECT (" + ",".join(_STATE_SELECT_COLUMNS) + ") "
        "ON TABLE public.org_branch_state TO app_security_owner"
    )
    op.execute(
        "GRANT UPDATE (" + ",".join(_STATE_UPDATE_COLUMNS) + ") "
        "ON TABLE public.org_branch_state TO app_security_owner"
    )
    op.execute(
        "GRANT SELECT (attempt_count) ON TABLE public.branch_outbox_events TO app_security_owner"
    )
    op.execute(
        "GRANT UPDATE (" + ",".join(_OUTBOX_UPDATE_COLUMNS) + ") "
        "ON TABLE public.branch_outbox_events TO app_security_owner"
    )
    op.execute(
        "GRANT SELECT (attempt_id,outbox_id,tenant_id,branch_id,desired_version,operation,"
        "attempt_number,outcome,provider_code,provider_index,provider_document_id,request_sha256,"
        "provider_version,provider_evidence_sha256,document_sha256,error_code,created_at) "
        "ON TABLE public.branch_search_effect_attempts TO app_security_owner"
    )
    op.execute(
        "GRANT INSERT (attempt_id,outbox_id,tenant_id,branch_id,desired_version,operation,"
        "attempt_number,outcome,provider_code,provider_index,provider_document_id,request_sha256,"
        "provider_version,provider_evidence_sha256,document_sha256,error_code,created_at) "
        "ON TABLE public.branch_search_effect_attempts TO app_security_owner"
    )

    op.execute(
        "CREATE POLICY p4b_search_internal_branch_read ON public.org_branches "
        "FOR SELECT TO app_security_owner USING (TRUE)"
    )
    op.execute(
        "CREATE POLICY p4b_search_internal_state_read ON public.org_branch_state "
        "FOR SELECT TO app_security_owner USING (TRUE)"
    )
    op.execute(
        "CREATE POLICY p4b_search_internal_state_update ON public.org_branch_state "
        "FOR UPDATE TO app_security_owner USING (TRUE) WITH CHECK (TRUE)"
    )
    op.execute(
        "CREATE POLICY p4b_search_attempt_read ON public.branch_search_effect_attempts "
        "FOR SELECT TO app_security_owner USING (TRUE)"
    )
    op.execute(
        "CREATE POLICY p4b_search_attempt_insert ON public.branch_search_effect_attempts "
        "FOR INSERT TO app_security_owner WITH CHECK (TRUE)"
    )
    op.execute(
        "CREATE POLICY p4b_search_internal_outbox_update ON public.branch_outbox_events "
        "FOR UPDATE TO app_security_owner USING (TRUE) WITH CHECK (TRUE)"
    )

    _create_secure_functions()

    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("GRANT USAGE ON SCHEMA app_secure TO migration_owner")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.bump_branch_search_version_from_state() "
        "TO migration_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_secure.bump_branch_search_version_from_branch() "
        "TO migration_owner"
    )
    op.execute("RESET ROLE")
    op.execute(
        """
        CREATE TRIGGER trg_p4b_search_version_state
        BEFORE UPDATE OF status, is_operational, is_public, deleted_at
        ON public.org_branch_state
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.bump_branch_search_version_from_state()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_p4b_search_version_branch
        AFTER UPDATE OF branch_name, internal_slug, timezone, region_code, country_code
        ON public.org_branches
        FOR EACH ROW
        EXECUTE FUNCTION app_secure.bump_branch_search_version_from_branch()
        """
    )
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app_secure.bump_branch_search_version_from_state() "
        "FROM migration_owner"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION app_secure.bump_branch_search_version_from_branch() "
        "FROM migration_owner"
    )
    op.execute("REVOKE USAGE ON SCHEMA app_secure FROM migration_owner")
    op.execute("RESET ROLE")

    _require_no_new_runtime_table_acl(bind)
    for role_name in ("app_runtime", "auth_runtime", _MAINTENANCE):
        for signature in _FUNCTIONS[:3]:
            if bind.execute(
                sa.text(
                    "SELECT pg_catalog.has_function_privilege("
                    ":role, CAST(:sig AS regprocedure), 'EXECUTE')"
                ),
                {"role": role_name, "sig": signature},
            ).scalar_one():
                raise RuntimeError(f"u07 leaked worker search capability to {role_name}")
    if bind.execute(
        sa.text(
            "SELECT pg_catalog.has_function_privilege('worker_runtime', "
            "CAST(:sig AS regprocedure), 'EXECUTE')"
        ),
        {"sig": _FUNCTIONS[3]},
    ).scalar_one():
        raise RuntimeError("u07 leaked global reconciliation capability to worker_runtime")


def downgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)

    live = bind.execute(
        sa.text(
            """
            SELECT count(*) FROM public.branch_outbox_events
            WHERE event_type IN ('branch.search_index','branch.search_deindex')
              AND status IN ('pending','processing')
            """
        )
    ).scalar_one()
    evidence = bind.execute(
        sa.text("SELECT count(*) FROM public.branch_search_effect_attempts")
    ).scalar_one()
    acknowledged = bind.execute(
        sa.text(
            "SELECT count(*) FROM public.org_branch_state "
            "WHERE search_provider_ack_version IS NOT NULL"
        )
    ).scalar_one()
    if live or evidence or acknowledged:
        raise RuntimeError(
            "u07 downgrade refuses loss of live/provider-backed search state: "
            f"live={live}, evidence={evidence}, acknowledged={acknowledged}"
        )

    op.execute("DROP TRIGGER trg_p4b_search_version_branch ON public.org_branches")
    op.execute("DROP TRIGGER trg_p4b_search_version_state ON public.org_branch_state")

    op.execute("SET LOCAL ROLE app_security_owner")
    for signature in (*_FUNCTIONS, *_TRIGGER_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("RESET ROLE")

    op.execute("DROP POLICY p4b_search_internal_outbox_update ON public.branch_outbox_events")
    op.execute("DROP POLICY p4b_search_attempt_insert ON public.branch_search_effect_attempts")
    op.execute("DROP POLICY p4b_search_attempt_read ON public.branch_search_effect_attempts")
    op.execute("DROP POLICY p4b_search_internal_state_update ON public.org_branch_state")
    op.execute("DROP POLICY p4b_search_internal_state_read ON public.org_branch_state")
    op.execute("DROP POLICY p4b_search_internal_branch_read ON public.org_branches")

    op.execute(
        "REVOKE SELECT (" + ",".join(_BRANCH_SELECT_COLUMNS) + ") "
        "ON TABLE public.org_branches FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT (" + ",".join(_STATE_SELECT_COLUMNS) + ") "
        "ON TABLE public.org_branch_state FROM app_security_owner"
    )
    op.execute(
        "REVOKE UPDATE (" + ",".join(_STATE_UPDATE_COLUMNS) + ") "
        "ON TABLE public.org_branch_state FROM app_security_owner"
    )
    op.execute(
        "REVOKE SELECT (attempt_count) ON TABLE public.branch_outbox_events "
        "FROM app_security_owner"
    )
    op.execute(
        "REVOKE UPDATE (" + ",".join(_OUTBOX_UPDATE_COLUMNS) + ") "
        "ON TABLE public.branch_outbox_events FROM app_security_owner"
    )

    op.execute("DROP TABLE public.branch_search_effect_attempts")
    op.execute(
        """
        ALTER TABLE public.org_branch_state
            DROP CONSTRAINT chk_branch_search_provider_ack_shape,
            DROP CONSTRAINT chk_branch_search_provider_evidence_hash,
            DROP CONSTRAINT chk_branch_search_provider_document_hash,
            DROP CONSTRAINT chk_branch_search_provider_ack_version,
            DROP COLUMN search_provider_reconciled_at,
            DROP COLUMN search_provider_acknowledged_at,
            DROP COLUMN search_provider_document_id,
            DROP COLUMN search_provider_index,
            DROP COLUMN search_provider_code,
            DROP COLUMN search_provider_evidence_sha256,
            DROP COLUMN search_provider_document_hash,
            DROP COLUMN search_provider_ack_version
        """
    )
