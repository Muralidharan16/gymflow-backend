from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "d7e8f9a0b1c2_current_organization_slug_capability.py"
SERVICE = ROOT / "app" / "services" / "member_subscription_v2_service.py"


def test_subscription_service_uses_only_current_slug_capability() -> None:
    source = SERVICE.read_text()
    tree = ast.parse(source)

    assert "from app.models.organization import Organization" not in source
    assert "session.get(Organization" not in source
    assert "self.session.get(Organization" not in source
    assert "func.public.current_organization_slug()" in source
    assert "_clean_org_prefix(org_slug)" in source

    imported_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Organization" not in imported_names


def test_slug_capability_does_not_grant_runtime_organization_table_read() -> None:
    source = MIGRATION.read_text()
    upper = source.upper()

    assert "GRANT SELECT (ID, SLUG) ON TABLE PUBLIC.ORGANIZATIONS TO APP_SECURITY_OWNER" in upper
    assert "GRANT SELECT ON TABLE PUBLIC.ORGANIZATIONS TO APP_RUNTIME" not in upper
    assert "GRANT SELECT (ID, SLUG) ON TABLE PUBLIC.ORGANIZATIONS TO APP_RUNTIME" not in upper
    assert "HAS_TABLE_PRIVILEGE(:ROLE_NAME, 'PUBLIC.ORGANIZATIONS', 'SELECT')" in upper
    for sensitive in ("tier", "max_branches", "verification_status", "document_url"):
        assert sensitive in source


def test_slug_capability_is_tenant_bound_security_definer_with_closed_execute_acl() -> None:
    source = MIGRATION.read_text()
    normalized = " ".join(source.split())

    assert "CREATE FUNCTION public.current_organization_slug()" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog" in source
    assert "SET row_security = on" in source
    assert "app.current_org_id" in source
    assert "organization.id" in source
    assert "organization.slug" in source
    assert "REVOKE ALL ON FUNCTION public.current_organization_slug() FROM PUBLIC" in normalized
    assert "GRANT EXECUTE ON FUNCTION public.current_organization_slug() TO app_runtime" in normalized
    assert "app_security_owner" in source


def test_slug_capability_has_bounded_create_window_and_exact_downgrade() -> None:
    source = MIGRATION.read_text()
    normalized = " ".join(source.split())
    tree = ast.parse(source)

    assert "GRANT CREATE ON SCHEMA public TO app_security_owner" in normalized
    assert "REVOKE CREATE ON SCHEMA public FROM app_security_owner" in normalized
    assert "DROP FUNCTION public.current_organization_slug() RESTRICT" in normalized
    assert "REVOKE SELECT (id, slug) ON TABLE public.organizations FROM app_security_owner" in normalized

    sql_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    for literal in sql_literals:
        assert not re.search(
            r"\b(?:CREATE|ALTER)\s+ROLE\b[^;]*\bBYPASSRLS\b",
            literal,
            re.IGNORECASE | re.DOTALL,
        )

    for forbidden in (
        "GRANT ALL",
        "GRANT DELETE",
        "GRANT UPDATE ON TABLE public.organizations TO app_runtime",
        "ALTER ROLE app_runtime",
        "OWNER TO app_runtime",
    ):
        assert forbidden not in normalized
