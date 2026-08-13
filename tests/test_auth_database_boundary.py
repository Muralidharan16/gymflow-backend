from __future__ import annotations

from pathlib import Path


CONFIG = Path("app/core/config.py").read_text(encoding="utf-8")
AUTH_DB = Path("app/core/auth_database.py").read_text(encoding="utf-8")
AUTH_ROUTER = Path("app/routers/auth.py").read_text(encoding="utf-8")
ONBOARDING = Path("app/routers/onboarding.py").read_text(encoding="utf-8")
MIGRATION = Path(
    "alembic/versions/5e6f708192a3_auth_runtime_privilege_boundary.py"
).read_text(encoding="utf-8")
ROLES = Path("security/cluster_role_bootstrap/roles.v1.json").read_text(
    encoding="utf-8"
)


def test_production_requires_distinct_auth_database_identity() -> None:
    assert "AUTH_DATABASE_URL: str" in CONFIG
    assert 'if self.ENVIRONMENT == "production"' in CONFIG
    assert "AUTH_DATABASE_URL is required in production" in CONFIG
    assert "validate_runtime_url_configuration" in CONFIG
    assert '"auth": self.AUTH_DATABASE_URL' in CONFIG

    assert "make_url(settings.DATABASE_URL)" in AUTH_DB
    assert "make_url(raw)" in AUTH_DB
    assert "ordinary.database != auth.database" in AUTH_DB
    assert "ordinary.username == auth.username" in AUTH_DB
    assert "initialize_request_session(session, request)" in AUTH_DB


def test_auth_write_routes_use_auth_pool_but_read_routes_do_not() -> None:
    for route_marker in (
        '@router.post("/signup")',
        '@router.get("/verify")',
        '@router.post("/resend-verification")',
        '@router.post("/login")',
        '@router.post("/refresh")',
    ):
        block = AUTH_ROUTER[AUTH_ROUTER.index(route_marker) :]
        next_route = block.find("\n@router.", len(route_marker))
        if next_route != -1:
            block = block[:next_route]
        assert "Depends(get_auth_db)" in block

    me = AUTH_ROUTER[AUTH_ROUTER.index('@router.get("/me")') :]
    assert "Depends(get_db)" in me
    assert "Depends(get_auth_db)" not in me

    complete = ONBOARDING[ONBOARDING.index('@router.post("/complete"') :]
    status_index = complete.index('@router.get("/status"')
    complete = complete[:status_index]
    assert "Depends(get_auth_db)" in complete

    status_block = ONBOARDING[ONBOARDING.index('@router.get("/status"') :]
    assert "Depends(get_db)" in status_block
    assert "Depends(get_auth_db)" not in status_block


def test_auth_runtime_is_managed_nonlogin_and_not_migration_membership() -> None:
    assert '"auth_runtime"' in ROLES
    assert '"can_login": false' in ROLES
    assert '"bypass_rls": false' in ROLES
    assert "must never own schema objects" in ROLES

    assert "CREATE ROLE" not in MIGRATION
    assert "ALTER ROLE" not in MIGRATION
    assert "DROP ROLE" not in MIGRATION
    assert "migration_owner must never be a member of auth_runtime" in MIGRATION


def test_auth_acl_surface_is_explicit_and_non_destructive() -> None:
    expected = {
        '"public.organizations": ("SELECT", "INSERT", "UPDATE")',
        '"public.owners": ("SELECT", "INSERT", "UPDATE")',
        '"public.gyms": ("SELECT", "INSERT", "UPDATE")',
        '"public.facility_types": ("SELECT",)',
        '"public.gym_facility_types": ("SELECT", "INSERT")',
        '"public.auth_session_families": ("SELECT", "INSERT", "UPDATE")',
        '"public.auth_sessions": ("SELECT", "INSERT", "UPDATE")',
        '"public.trial_subscriptions": ("SELECT", "INSERT")',
        '"public.audit_logs": ("INSERT",)',
    }
    for token in expected:
        assert token in MIGRATION

    assert '"DELETE"' in MIGRATION
    assert '"TRUNCATE"' in MIGRATION
    assert "_FORBIDDEN_TABLE_PRIVILEGES" in MIGRATION
    assert "has_schema_privilege" in MIGRATION
    assert "'public', 'CREATE'" in MIGRATION
    assert "pre-existing table ACLs" in MIGRATION
    assert "direct table ACL drift" in MIGRATION


def test_auth_runtime_has_no_finance_or_platform_billing_acl() -> None:
    table_contract = MIGRATION[
        MIGRATION.index("_TABLE_PRIVILEGES") : MIGRATION.index(
            "_FORBIDDEN_TABLE_PRIVILEGES"
        )
    ]
    assert "finance." not in table_contract
    assert "platform_" not in table_contract
