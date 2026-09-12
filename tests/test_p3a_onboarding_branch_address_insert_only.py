from __future__ import annotations

import ast
from pathlib import Path


SERVICE = Path("app/services/onboarding_service.py")
ORG_BRANCH_MODEL = Path("app/models/org_branch.py")
ADDRESS_MODEL = Path("app/models/address.py")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_onboarding_reserves_one_address_id_before_branch_insert() -> None:
    source = _source(SERVICE)

    assert "branch_id = uuid.uuid4()" in source
    assert "address_id = uuid.uuid4()" in source
    assert "address_id=address_id" in source
    assert "id=address_id" in source
    assert "branch_id=branch_id" in source


def test_onboarding_never_mutates_branch_address_after_insert() -> None:
    source = _source(SERVICE)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]

        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "branch"
                and target.attr == "address_id"
            ):
                raise AssertionError(
                    "onboarding must not issue a post-INSERT org_branches.address_id mutation"
                )


def test_model_contract_allows_insert_only_link_order() -> None:
    branch_source = _source(ORG_BRANCH_MODEL)
    address_source = _source(ADDRESS_MODEL)

    # org_branches.address_id is deliberately not an FK, so a reserved address
    # UUID can be written on the initial branch INSERT. The real referential
    # constraint points from organization_addresses.branch_id back to the
    # already-persisted branch, which is why the branch is flushed first.
    branch_address_fragment = branch_source.split(
        "address_id: Mapped[Optional[uuid.UUID]]", 1
    )[1].split("branch_metadata", 1)[0]
    assert "ForeignKey(" not in branch_address_fragment

    address_branch_fragment = address_source.split(
        "branch_id: Mapped[uuid.UUID]", 1
    )[1].split("address_type", 1)[0]
    assert 'ForeignKey("org_branches.id"' in address_branch_fragment


def test_p3a_does_not_compensate_with_auth_branch_update_grant() -> None:
    migration_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path("alembic/versions").glob("c?7d8e9f0a*.py"))
    )

    assert "GRANT UPDATE ON TABLE public.org_branches TO auth_runtime" not in migration_sources
    assert "GRANT UPDATE ON public.org_branches TO auth_runtime" not in migration_sources
