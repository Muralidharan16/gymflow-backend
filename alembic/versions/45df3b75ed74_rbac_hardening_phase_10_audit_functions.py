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


def upgrade() -> None:
    # 1. Advisory Lock Helper
    op.execute("DROP FUNCTION IF EXISTS app_private.org_advisory_lock_key(uuid);")
    op.execute("""
    CREATE OR REPLACE FUNCTION app_private.org_advisory_lock_key(org_id UUID)
    RETURNS BIGINT STRICT IMMUTABLE PARALLEL SAFE SECURITY DEFINER
    SET search_path = pg_catalog AS $$
    BEGIN
        RETURN (('x' || substr(md5(org_id::text), 1, 16)))::bit(64)::bigint;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("ALTER FUNCTION app_private.org_advisory_lock_key(uuid) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.org_advisory_lock_key(uuid) FROM PUBLIC;")

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

    op.execute("ALTER FUNCTION app_private.append_audit_event(uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, text, jsonb, uuid, text, varchar) OWNER TO app_security_owner;")
    op.execute("REVOKE ALL ON FUNCTION app_private.append_audit_event(uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, text, jsonb, uuid, text, varchar) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_private.append_audit_event(uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, text, jsonb, uuid, text, varchar) TO audit_writer;")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_private.append_audit_event(uuid, uuid, uuid, jsonb, jsonb, varchar, varchar, text, jsonb, uuid, text, varchar);")
    op.execute("DROP FUNCTION IF EXISTS app_private.org_advisory_lock_key(uuid);")
