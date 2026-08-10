from __future__ import annotations

import ast
from pathlib import Path


ROUTER = Path("app/routers/branch_lifecycle.py")


def _source() -> str:
    return ROUTER.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
    module = ast.parse(source, filename=str(ROUTER))
    node = next(
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_branch_list_uses_branch_contacts_as_canonical_contact_source() -> None:
    source = _function_source("list_branches")

    assert "BranchContactORM" in source
    assert "ContactKind.PHONE" in source
    assert "ContactKind.EMAIL" in source
    assert "BranchContactORM.org_id == current_staff.org_id" in source
    assert "BranchContactORM.is_primary == True" in source

    # Branch listing must not expand its database privilege surface merely to
    # synthesize contact fallbacks. Organization profile data has a separate
    # tenant-root security boundary, and the requesting staff member is not the
    # branch's public contact identity.
    assert "from app.models.organization import Organization" not in source
    assert "select(Organization)" not in source
    assert "current_staff.email" not in source

    assert 'contacts_dict.get("email") or f"hello@{branch.internal_slug}.com"' in source
    assert 'contacts_dict.get("phone") or "Pending Setup"' in source
