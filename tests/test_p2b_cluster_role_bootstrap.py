from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.core.cluster_role_bootstrap import (
    BootstrapContractError,
    render_fresh_cluster_bootstrap,
)
from app.core.cluster_role_contract import ContractBundle, load_contract_bundle
from app.core.cluster_role_preflight import ExternalRoleCatalog, evaluate_cluster_role_catalog
from scripts.verify_head_workflow_bootstrap import (
    CANONICAL_BOOTSTRAP,
    inspect_workflow_text,
    scan_repository,
)


ROOT = Path(__file__).resolve().parents[1]


def _replace_bundle(bundle: ContractBundle, **changes: object) -> ContractBundle:
    values = {
        "roles": bundle.roles,
        "role_settings": bundle.role_settings,
        "memberships": bundle.memberships,
        "grantors": bundle.grantors,
        "ownership": bundle.ownership,
    }
    values.update(changes)
    return ContractBundle(**values)


def test_fresh_bootstrap_is_exact_manifest_driven_and_create_only() -> None:
    bundle = load_contract_bundle()
    sql = render_fresh_cluster_bootstrap(bundle)
    managed = bundle.roles["managed_roles"]

    assert "BEGIN;" in sql
    assert sql.rstrip().endswith("COMMIT;")
    assert "current_user = 'postgres'" not in sql
    assert "current_user <> 'postgres'" in sql
    assert "session_user <> 'postgres'" in sql
    assert "NOT operator_record.rolsuper" in sql
    assert "refuses existing managed/retired roles" in sql

    for role in managed:
        assert sql.count(f"CREATE ROLE {role} ") == 1

    assert "CREATE ROLE lifecycle_maintenance_runtime" in sql
    for setting, value in {
        "statement_timeout": "15s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "30s",
        "row_security": "on",
    }.items():
        assert (
            f"ALTER ROLE lifecycle_maintenance_runtime SET {setting} = '{value}';"
            in sql
        )

    assert (
        "GRANT app_rls_executor TO migration_owner "
        "WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;"
    ) in sql
    assert (
        "GRANT app_security_owner TO migration_owner "
        "WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;"
    ) in sql

    for forbidden in (
        "CREATE DATABASE",
        "GRANT CONNECT ON DATABASE",
        "GRANT USAGE ON SCHEMA",
        "GRANT SELECT",
        "GRANT INSERT",
        "app_test_runtime",
        "auth_test_runtime",
        "worker_test_runtime",
        "maintenance_test_runtime",
        "test_runner",
    ):
        assert forbidden not in sql


def test_internal_billing_worker_is_formally_retired_not_managed() -> None:
    bundle = load_contract_bundle()
    retired = bundle.roles["retired_roles"]

    assert "internal_billing_worker" not in bundle.roles["managed_roles"]
    assert retired["internal_billing_worker"]["expected_presence"] is False
    assert retired["internal_billing_worker"]["role"] == "internal_billing_worker"
    sql = render_fresh_cluster_bootstrap(bundle)
    assert "internal_billing_worker" in sql
    assert "CREATE ROLE internal_billing_worker" not in sql


def test_renderer_rejects_unsupported_managed_setting() -> None:
    bundle = load_contract_bundle()
    settings = deepcopy(bundle.role_settings)
    settings["settings_by_role"]["app_runtime"]["search_path"] = "public"

    with pytest.raises(BootstrapContractError, match="unsupported managed role setting"):
        render_fresh_cluster_bootstrap(_replace_bundle(bundle, role_settings=settings))


def test_renderer_rejects_wrong_membership_grantor() -> None:
    bundle = load_contract_bundle()
    memberships = deepcopy(bundle.memberships)
    memberships["exact_rows"][0]["approved_grantor"] = "migration_owner"

    with pytest.raises(BootstrapContractError, match="grantor/cardinality"):
        render_fresh_cluster_bootstrap(_replace_bundle(bundle, memberships=memberships))


def test_renderer_rejects_duplicate_membership_pair() -> None:
    bundle = load_contract_bundle()
    memberships = deepcopy(bundle.memberships)
    memberships["exact_rows"].append(deepcopy(memberships["exact_rows"][0]))

    with pytest.raises(BootstrapContractError, match="duplicate membership contract pair"):
        render_fresh_cluster_bootstrap(_replace_bundle(bundle, memberships=memberships))


def test_renderer_rejects_unsafe_role_identifier() -> None:
    bundle = load_contract_bundle()
    roles = deepcopy(bundle.roles)
    record = roles["managed_roles"].pop("app_runtime")
    roles["managed_roles"]["app_runtime;DROP_ROLE"] = record

    with pytest.raises(BootstrapContractError, match="unsafe PostgreSQL identifier"):
        render_fresh_cluster_bootstrap(_replace_bundle(bundle, roles=roles))


def test_repository_head_jobs_have_canonical_bootstrap_first() -> None:
    assert scan_repository(ROOT) == ()


def test_workflow_guard_rejects_head_without_bootstrap() -> None:
    bundle = load_contract_bundle()
    text = """jobs:\n  bad:\n    steps:\n      - run: python -m alembic upgrade head\n"""
    violations = inspect_workflow_text(
        "bad.yml", text, managed_roles=set(bundle.roles["managed_roles"])
    )
    assert any(v.code == "head.bootstrap_cardinality" for v in violations)


def test_workflow_guard_rejects_bootstrap_after_head() -> None:
    bundle = load_contract_bundle()
    text = f"""jobs:\n  bad:\n    steps:\n      - run: python -m alembic upgrade head\n      - run: {CANONICAL_BOOTSTRAP}\n"""
    violations = inspect_workflow_text(
        "bad.yml", text, managed_roles=set(bundle.roles["managed_roles"])
    )
    assert any(v.code == "head.bootstrap_order" for v in violations)


def test_workflow_guard_rejects_manual_managed_role_maintenance() -> None:
    bundle = load_contract_bundle()
    text = f"""jobs:\n  bad:\n    steps:\n      - run: {CANONICAL_BOOTSTRAP}\n      - run: |\n          CREATE ROLE app_runtime NOLOGIN;\n          ALTER ROLE worker_runtime SET statement_timeout = '15s';\n          GRANT app_rls_executor TO migration_owner WITH SET TRUE;\n      - run: python -m alembic upgrade head\n"""
    violations = inspect_workflow_text(
        "bad.yml", text, managed_roles=set(bundle.roles["managed_roles"])
    )
    codes = {v.code for v in violations}
    assert "head.manual_managed_role_create" in codes
    assert "head.manual_managed_role_setting" in codes
    assert "head.manual_migration_membership" in codes


def test_workflow_guard_allows_intentional_historical_partial_job() -> None:
    bundle = load_contract_bundle()
    text = """jobs:\n  historical:\n    steps:\n      - run: |\n          CREATE ROLE migration_owner LOGIN;\n          python -m alembic upgrade 0009_view_security_invoker\n"""
    assert (
        inspect_workflow_text(
            "historical.yml", text, managed_roles=set(bundle.roles["managed_roles"])
        )
        == ()
    )


def _catalog_from_bundle(bundle: ContractBundle) -> ExternalRoleCatalog:
    roles = []
    for role, record in bundle.roles["managed_roles"].items():
        roles.append({"role": role, **record["attributes"]})
    memberships = [
        {
            "granted_role": row["granted_role"],
            "member_role": row["member_role"],
            "grantor": row["approved_grantor"],
            "set_option": row["set_option"],
            "inherit_option": row["inherit_option"],
            "admin_option": row["admin_option"],
        }
        for row in bundle.memberships["exact_rows"]
    ]
    return ExternalRoleCatalog(
        current_user="postgres",
        session_user="postgres",
        snapshot={
            "roles": roles,
            "role_settings": deepcopy(bundle.role_settings["settings_by_role"]),
            "memberships": memberships,
            "objects": [],
        },
    )


def _violation_codes(catalog: ExternalRoleCatalog, bundle: ContractBundle) -> set[str]:
    return {v.code for v in evaluate_cluster_role_catalog(catalog, bundle)}


def test_read_only_verifier_rejects_all_p2b_negative_contract_classes() -> None:
    bundle = load_contract_bundle()

    missing = _catalog_from_bundle(bundle)
    missing.snapshot["roles"][:] = [
        row for row in missing.snapshot["roles"]
        if row["role"] != "lifecycle_maintenance_runtime"
    ]
    assert _violation_codes(missing, bundle)

    wrong_bypass = _catalog_from_bundle(bundle)
    next(row for row in wrong_bypass.snapshot["roles"] if row["role"] == "worker_runtime")[
        "bypass_rls"
    ] = True
    assert _violation_codes(wrong_bypass, bundle)

    wrong_login = _catalog_from_bundle(bundle)
    next(
        row for row in wrong_login.snapshot["roles"]
        if row["role"] == "lifecycle_maintenance_runtime"
    )["can_login"] = True
    assert _violation_codes(wrong_login, bundle)

    wrong_inherit = _catalog_from_bundle(bundle)
    next(row for row in wrong_inherit.snapshot["roles"] if row["role"] == "app_runtime")[
        "inherit"
    ] = True
    assert _violation_codes(wrong_inherit, bundle)

    wrong_timeout = _catalog_from_bundle(bundle)
    wrong_timeout.snapshot["role_settings"]["lifecycle_maintenance_runtime"][
        "statement_timeout"
    ] = "16s"
    assert _violation_codes(wrong_timeout, bundle)

    extra_membership = _catalog_from_bundle(bundle)
    extra_membership.snapshot["memberships"].append(
        {
            "granted_role": "app_runtime",
            "member_role": "migration_owner",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        }
    )
    assert _violation_codes(extra_membership, bundle)

    wrong_grantor = _catalog_from_bundle(bundle)
    wrong_grantor.snapshot["memberships"][0]["grantor"] = "p2b_wrong_grantor"
    assert _violation_codes(wrong_grantor, bundle)

    wrong_options = _catalog_from_bundle(bundle)
    wrong_options.snapshot["memberships"][0]["inherit_option"] = True
    wrong_options.snapshot["memberships"][0]["set_option"] = False
    wrong_options.snapshot["memberships"][0]["admin_option"] = True
    assert _violation_codes(wrong_options, bundle)

    duplicate_rows = _catalog_from_bundle(bundle)
    duplicate_rows.snapshot["memberships"].append(
        deepcopy(duplicate_rows.snapshot["memberships"][0])
    )
    assert _violation_codes(duplicate_rows, bundle)

    database_override = _catalog_from_bundle(bundle)
    database_override = ExternalRoleCatalog(
        current_user=database_override.current_user,
        session_user=database_override.session_user,
        snapshot=database_override.snapshot,
        database_setting_overrides=(
            {
                "role": "app_runtime",
                "database": "gymflow_p2b_bootstrap",
                "setting": "statement_timeout=1s",
            },
        ),
    )
    assert "preflight.database_role_setting" in _violation_codes(database_override, bundle)

    retired_present = _catalog_from_bundle(bundle)
    retired_present.snapshot["roles"].append(
        {
            "role": "internal_billing_worker",
            "superuser": False,
            "inherit": False,
            "create_role": False,
            "create_db": False,
            "can_login": False,
            "replication": False,
            "bypass_rls": False,
        }
    )
    assert "role.retired_present" in _violation_codes(retired_present, bundle)
