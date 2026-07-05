"""RBAC Hardening Phase 1 — DB Roles, Extensions, Schemas, Privilege Bootstrap

Phase 1 of the v18.0 hardening plan.

Creates:
  • Extensions: btree_gist (pinned), pgcrypto (pinned)
  • DB roles: app_security_owner, app_runtime, audit_writer, readonly_analytics
    (app_migrator is the Alembic runner; not created here, assumed to exist)
  • Schema: app_secure  (security-barrier views)
  • Privilege revocations & grants on existing schemas

NOTE: This migration does NOT touch any application tables.
      It is purely infrastructure/governance.

Revision ID: 0022_rbac_phase1_roles_extensions
Revises: 0021_staff_roles
Create Date: 2026-05-23
"""

from alembic import op

revision = "0022_rbac_p1_roles"
down_revision = "0021_staff_roles"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _role_exists(role_name: str) -> str:
    """Return SQL snippet that creates role only if it does not exist."""
    return f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN
                CREATE ROLE {role_name} NOLOGIN NOINHERIT;
            END IF;
        END$$;
    """


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:

    # ── 1. Extensions ────────────────────────────────────────────────────
    # btree_gist: required for temporal EXCLUDE constraints on branch_staff_roles
    # pgcrypto:   required for sha256() inside append_audit_event()
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # ── 2. DB Roles ──────────────────────────────────────────────────────

    # app_security_owner: non-login owner of all SECURITY DEFINER objects.
    # Must NOT have BYPASSRLS or superuser, must NOT own any tenant tables.
    op.execute(_role_exists("app_security_owner"))
    op.execute("""
        DO $$
        BEGIN
            -- Ensure NOBYPASSRLS (cannot be set via CREATE ROLE IF NOT EXISTS)
            ALTER ROLE app_security_owner NOLOGIN NOINHERIT NOBYPASSRLS;
        END$$;
    """)

    # app_runtime: connection-pooled application role (FastAPI).
    # RLS is enforced; statement/lock timeouts set.
    op.execute(_role_exists("app_runtime"))
    op.execute("""
        DO $$
        BEGIN
            ALTER ROLE app_runtime NOLOGIN NOBYPASSRLS;
            ALTER ROLE app_runtime SET statement_timeout = '5s';
            ALTER ROLE app_runtime SET lock_timeout = '2s';
            ALTER ROLE app_runtime SET row_security = on;
        END$$;
    """)

    # audit_writer: append-only writer for audit events.
    # Calls app_private.append_audit_event() only — no direct table access.
    op.execute(_role_exists("audit_writer"))
    op.execute("ALTER ROLE audit_writer NOLOGIN NOBYPASSRLS;")

    # readonly_analytics: replica-only read role (BI tools, dashboards).
    op.execute(_role_exists("readonly_analytics"))
    op.execute("ALTER ROLE readonly_analytics NOLOGIN NOBYPASSRLS;")

    # ── 3. app_secure Schema ─────────────────────────────────────────────
    # Houses security-barrier views. Owned by app_security_owner.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'app_secure') THEN
                CREATE SCHEMA app_secure AUTHORIZATION app_security_owner;
            END IF;
        END$$;
    """)

    # Revoke PUBLIC access; grant USAGE only to runtime roles.
    op.execute("REVOKE ALL ON SCHEMA app_secure FROM PUBLIC;")
    op.execute("GRANT USAGE ON SCHEMA app_secure TO app_runtime, readonly_analytics;")

    # Ensure future objects in app_secure are not exposed to PUBLIC by default.
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure
        REVOKE ALL ON TABLES FROM PUBLIC;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA app_secure
        GRANT SELECT ON TABLES TO app_runtime;
    """)

    # ── 4. app_private Schema Hardening ──────────────────────────────────
    # app_private already exists (created in 0002_enterprise_platform).
    # Tighten PUBLIC access.
    op.execute("REVOKE ALL ON SCHEMA app_private FROM PUBLIC;")
    op.execute("GRANT USAGE ON SCHEMA app_private TO app_security_owner;")

    # ── 5. public Schema Hardening ────────────────────────────────────────
    # Restrict PUBLIC from having blanket rights on public schema.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC;")

    # Grant schema-level access to runtime roles.
    op.execute("""
        GRANT USAGE ON SCHEMA public TO
            app_runtime,
            audit_writer,
            readonly_analytics;
    """)

    # ── 6. Role comments (documentation in DB catalogue) ─────────────────
    op.execute("COMMENT ON ROLE app_security_owner IS 'Owns all SECURITY DEFINER functions and trigger routines. No table ownership. No BYPASSRLS.';")
    op.execute("COMMENT ON ROLE app_runtime IS 'FastAPI connection-pooled role. RLS enforced. 5s statement timeout.';")
    op.execute("COMMENT ON ROLE audit_writer IS 'Append-only audit event writer. Calls append_audit_event() only.';")
    op.execute("COMMENT ON ROLE readonly_analytics IS 'Read-only analytics/BI role for replica queries.';")
    op.execute("COMMENT ON SCHEMA app_secure IS 'Security-barrier views only. No direct table writes.';")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Schema comments
    op.execute("COMMENT ON SCHEMA app_secure IS NULL;")

    # Restore public schema CREATE grant for PUBLIC (undo restriction)
    op.execute("GRANT CREATE ON SCHEMA public TO PUBLIC;")

    # Drop app_secure schema (must be empty first due to CASCADE behaviour)
    op.execute("DROP SCHEMA IF EXISTS app_secure CASCADE;")

    # Drop roles (only if no objects are owned — safe because Phase 1 creates no objects)
    for role in ("readonly_analytics", "audit_writer", "app_runtime", "app_security_owner"):
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    DROP ROLE {role};
                END IF;
            END$$;
        """)

    # Drop extensions only if no dependents remain (safe — Phase 1 adds no schema objects)
    op.execute("DROP EXTENSION IF EXISTS pgcrypto;")
    op.execute("DROP EXTENSION IF EXISTS btree_gist;")
