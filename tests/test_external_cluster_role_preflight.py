from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.cluster_role_contract import load_contract_bundle
from app.core.cluster_role_preflight import (
    ExternalRoleCatalog,
    evaluate_external_role_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_MODULE = ROOT / "app/core/cluster_role_preflight.py"


def _exact_catalog() -> tuple[object, ExternalRoleCatalog]:
    bundle = load_contract_bundle()
    role_rows = [
        {"role": role, **record["attributes"]}
        for role, record in bundle.roles["managed_roles"].items()
    ]
    membership_rows = [
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
    catalog = ExternalRoleCatalog(
        current_user="migration_owner",
        session_user="migration_owner",
        snapshot={
            "roles": role_rows,
            "role_settings": {
                role: dict(settings)
                for role, settings in bundle.role_settings["settings_by_role"].items()
            },
            "memberships": membership_rows,
            "objects": [],
        },
    )
    return bundle, catalog


def test_preflight_accepts_exact_manifest_projection() -> None:
    bundle, catalog = _exact_catalog()
    assert evaluate_external_role_catalog(catalog, bundle) == ()


def test_preflight_rejects_extra_migration_owner_membership() -> None:
    bundle, catalog = _exact_catalog()
    snapshot = dict(catalog.snapshot)
    snapshot["memberships"] = [
        *snapshot["memberships"],
        {
            "granted_role": "test_runner",
            "member_role": "migration_owner",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        },
    ]
    drifted = ExternalRoleCatalog(
        current_user=catalog.current_user,
        session_user=catalog.session_user,
        snapshot=snapshot,
    )

    violations = evaluate_external_role_catalog(drifted, bundle)
    assert any(
        violation.code == "membership.forbidden"
        and violation.subject == "test_runner->migration_owner"
        for violation in violations
    )


def test_preflight_rejects_wrong_identity_and_database_override() -> None:
    bundle, catalog = _exact_catalog()
    drifted = ExternalRoleCatalog(
        current_user="postgres",
        session_user="postgres",
        snapshot=catalog.snapshot,
        database_setting_overrides=(
            {
                "role": "worker_runtime",
                "database": "gymflow_prod",
                "setting": "statement_timeout=60s",
            },
        ),
    )

    codes = {
        violation.code
        for violation in evaluate_external_role_catalog(drifted, bundle)
    }
    assert {
        "preflight.current_user",
        "preflight.session_user",
        "preflight.database_role_setting",
    } <= codes


def test_preflight_rejects_managed_role_setting_drift() -> None:
    bundle, catalog = _exact_catalog()
    snapshot = dict(catalog.snapshot)
    settings = {
        role: dict(values)
        for role, values in snapshot["role_settings"].items()
    }
    settings["app_runtime"]["lock_timeout"] = "30s"
    snapshot["role_settings"] = settings
    drifted = ExternalRoleCatalog(
        current_user=catalog.current_user,
        session_user=catalog.session_user,
        snapshot=snapshot,
    )

    assert any(
        violation.code == "role.settings"
        and violation.subject == "app_runtime"
        for violation in evaluate_external_role_catalog(drifted, bundle)
    )


def test_live_catalog_sql_is_select_only() -> None:
    source = PREFLIGHT_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    mutation = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|GRANT|REVOKE|TRUNCATE)\b",
        flags=re.IGNORECASE,
    )

    sql_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "SELECT" in node.value.upper()
    ]
    assert sql_literals
    for sql in sql_literals:
        assert mutation.search(sql) is None
