from __future__ import annotations

import ast
from pathlib import Path


SERVICE_PATH = Path("app/services/member_service.py")
CAPABILITY_MIGRATION_PATH = Path(
    "alembic/versions/d7e8f9a0b1c2_current_organization_slug_capability.py"
)


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1, f"expected exactly one class {name}, found {len(matches)}"
    return matches[0]


def _async_method(node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    matches = [
        child
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef) and child.name == name
    ]
    assert len(matches) == 1, f"expected exactly one async method {name}, found {len(matches)}"
    return matches[0]


def _calls_self_method(node: ast.AST, method_name: str) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == method_name
            and isinstance(function.value, ast.Name)
            and function.value.id == "self"
        ):
            return True
    return False


def test_member_service_uses_bounded_current_org_slug_capability() -> None:
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.models.organization", (
                "MemberService must not import the Organization ORM model under app_runtime"
            )
        if isinstance(node, ast.Name):
            assert node.id != "Organization", (
                "MemberService must not load tenant-root Organization rows under app_runtime"
            )

    service = _class_node(tree, "MemberService")
    helper = _async_method(service, "_current_organization_slug")
    helper_source = ast.unparse(helper)
    assert "current_organization_slug" in helper_source
    assert "self.session.scalar" in helper_source
    assert "NotFoundError" in helper_source

    protected_paths = (
        "create_member",
        "list_members_org",
        "get_member_org",
        "create_member_org",
    )
    for method_name in protected_paths:
        method = _async_method(service, method_name)
        assert _calls_self_method(method, "_current_organization_slug"), (
            f"{method_name} must derive member display codes through the bounded current-tenant slug capability"
        )


def test_current_org_slug_capability_does_not_open_organizations_table_to_runtime() -> None:
    source = CAPABILITY_MIGRATION_PATH.read_text(encoding="utf-8")
    normalized = " ".join(source.split()).lower()

    assert "security definer" in normalized
    assert "set search_path = pg_catalog" in normalized
    assert "set row_security = on" in normalized
    assert "app.current_org_id" in normalized
    assert "revoke execute on function public.current_organization_slug() from public" in normalized
    assert "grant execute on function public.current_organization_slug() to app_runtime" in normalized
    assert "grant select (id, slug) on table public.organizations to app_security_owner" in normalized

    forbidden_runtime_grants = (
        "grant select on table public.organizations to app_runtime",
        "grant update on table public.organizations to app_runtime",
        "grant delete on table public.organizations to app_runtime",
        "grant insert on table public.organizations to app_runtime",
    )
    for forbidden in forbidden_runtime_grants:
        assert forbidden not in normalized
