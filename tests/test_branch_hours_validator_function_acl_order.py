from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = Path(
    "alembic/versions/d6e7f8091a2b_align_branch_hours_typed_principals.py"
)


def _function_source(name: str) -> str:
    source = MIGRATION.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(MIGRATION))
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def test_validator_acl_is_finalized_as_actual_function_owner() -> None:
    body = _function_source("_create_nonmember_validator")

    owner_transfer = body.index("OWNER TO app_security_owner")
    set_owner = body.index('op.execute("SET LOCAL ROLE app_security_owner")')
    final_revoke = body.index("REVOKE ALL ON FUNCTION", set_owner)
    runtime_grant = body.index("GRANT EXECUTE ON FUNCTION", final_revoke)
    reset_owner = body.index('op.execute("RESET ROLE")', runtime_grant)

    assert owner_transfer < set_owner < final_revoke < runtime_grant < reset_owner

    # Revoke the PostgreSQL default PUBLIC EXECUTE before ownership transfer too;
    # the post-transfer owner-context revoke is the authoritative finalization.
    assert body.index("REVOKE ALL ON FUNCTION") < owner_transfer
    assert body.count("FROM PUBLIC") == 2


def test_forward_verifier_still_requires_public_execute_absent() -> None:
    verify = _function_source("_verify_forward")

    assert "pg_catalog.aclexplode" in verify
    assert "acl_data.grantee = 0" in verify
    assert "acl_data.privilege_type = 'EXECUTE'" in verify
    assert 'or function["public_execute"]' in verify
