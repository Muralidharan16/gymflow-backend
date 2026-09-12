from __future__ import annotations

from pathlib import Path

from app.core.cluster_role_contract import load_contract_bundle
from scripts.verify_head_workflow_bootstrap import (
    CANONICAL_BOOTSTRAP,
    TRUSTED_HEAD_WRAPPERS,
    inspect_head_wrapper_text,
    inspect_workflow_text,
)


ROOT = Path(__file__).resolve().parents[1]
P3E_WRAPPER_COMMAND = "bash scripts/ci/prepare_p3e_pg16.sh"
P3E_WRAPPER_PATH = "scripts/ci/prepare_p3e_pg16.sh"


def _managed_roles() -> set[str]:
    return set(load_contract_bundle().roles["managed_roles"])


def test_registered_p3e_head_wrapper_is_fixed_path_and_self_validating() -> None:
    assert TRUSTED_HEAD_WRAPPERS == {P3E_WRAPPER_COMMAND: P3E_WRAPPER_PATH}
    source = (ROOT / P3E_WRAPPER_PATH).read_text(encoding="utf-8")
    assert (
        inspect_head_wrapper_text(
            P3E_WRAPPER_PATH,
            source,
            managed_roles=_managed_roles(),
        )
        == ()
    )


def test_validated_wrapper_counts_as_single_bootstrap_and_head_boundary() -> None:
    text = f"""jobs:\n  wrapped:\n    steps:\n      - run: {P3E_WRAPPER_COMMAND}\n"""
    assert (
        inspect_workflow_text(
            "wrapped.yml",
            text,
            managed_roles=_managed_roles(),
            trusted_head_wrappers=(P3E_WRAPPER_COMMAND,),
        )
        == ()
    )


def test_validated_wrapper_then_later_head_still_has_one_bootstrap() -> None:
    text = f"""jobs:\n  wrapped:\n    steps:\n      - run: {P3E_WRAPPER_COMMAND}\n      - run: python -m alembic upgrade head\n"""
    assert (
        inspect_workflow_text(
            "wrapped.yml",
            text,
            managed_roles=_managed_roles(),
            trusted_head_wrappers=(P3E_WRAPPER_COMMAND,),
        )
        == ()
    )


def test_workflow_guard_rejects_direct_bootstrap_plus_validated_wrapper() -> None:
    text = f"""jobs:\n  duplicate:\n    steps:\n      - run: {CANONICAL_BOOTSTRAP}\n      - run: {P3E_WRAPPER_COMMAND}\n"""
    violations = inspect_workflow_text(
        "duplicate.yml",
        text,
        managed_roles=_managed_roles(),
        trusted_head_wrappers=(P3E_WRAPPER_COMMAND,),
    )
    assert any(v.code == "head.bootstrap_cardinality" for v in violations)


def test_wrapper_guard_rejects_missing_or_duplicate_bootstrap() -> None:
    missing = "python -m alembic upgrade head\n"
    missing_violations = inspect_head_wrapper_text(
        "missing.sh",
        missing,
        managed_roles=_managed_roles(),
    )
    assert any(v.code == "wrapper.bootstrap_cardinality" for v in missing_violations)

    duplicate = (
        f"{CANONICAL_BOOTSTRAP}\n"
        f"{CANONICAL_BOOTSTRAP}\n"
        "python -m alembic upgrade head\n"
    )
    duplicate_violations = inspect_head_wrapper_text(
        "duplicate.sh",
        duplicate,
        managed_roles=_managed_roles(),
    )
    assert any(v.code == "wrapper.bootstrap_cardinality" for v in duplicate_violations)


def test_wrapper_guard_rejects_bootstrap_after_head_and_manual_role_maintenance() -> None:
    source = (
        "python -m alembic upgrade head\n"
        f"{CANONICAL_BOOTSTRAP}\n"
        "CREATE ROLE app_runtime NOLOGIN;\n"
        "ALTER ROLE worker_runtime SET statement_timeout = '15s';\n"
        "GRANT app_security_owner TO migration_owner WITH SET TRUE;\n"
    )
    violations = inspect_head_wrapper_text(
        "bad.sh",
        source,
        managed_roles=_managed_roles(),
    )
    codes = {v.code for v in violations}
    assert "wrapper.bootstrap_order" in codes
    assert "wrapper.manual_managed_role_create" in codes
    assert "wrapper.manual_managed_role_setting" in codes
    assert "wrapper.manual_migration_membership" in codes


def test_wrapper_guard_rejects_registered_helper_without_head() -> None:
    source = f"{CANONICAL_BOOTSTRAP}\n"
    violations = inspect_head_wrapper_text(
        "no-head.sh",
        source,
        managed_roles=_managed_roles(),
    )
    assert any(v.code == "wrapper.head_missing" for v in violations)
