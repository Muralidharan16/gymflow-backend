"""Closed app_secure migration inventory with the original hardening suite preserved.

The pre-P2D static contract is kept byte-for-byte in the non-collected baseline
module. Later hardening revisions extend only the closed sensitive-migration
inventory; every other hardening regression is re-exported unchanged.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

_BASELINE_PATH = pathlib.Path(__file__).with_name(
    "migration_app_secure_owner_context_boundary_baseline.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "_doers_app_secure_owner_context_baseline", _BASELINE_PATH
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Unable to load app_secure owner-context baseline")
_BASELINE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASELINE
_SPEC.loader.exec_module(_BASELINE)

for _name, _value in vars(_BASELINE).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

# Preserve the historical detector but extend the collected closed inventory to
# cover policy DDL introduced by later app_secure migrations.  This deliberately
# detects the DDL rather than omitting it from the allowlist.
_BASELINE_APP_SECURE_DDL_CATEGORIES = _app_secure_ddl_categories


def _app_secure_ddl_categories(path: pathlib.Path) -> set[str]:
    categories = set(_BASELINE_APP_SECURE_DDL_CATEGORIES(path))
    text = re.sub(r"\s+", " ", "\n".join(_string_constants(_tree(path))))
    if re.search(r"\bCREATE\s+POLICY\b", text, re.IGNORECASE):
        categories.add("create_policy")
    if re.search(r"\bDROP\s+POLICY\b", text, re.IGNORECASE):
        categories.add("drop_policy")
    return categories


_P2D_MIGRATION = "9e4f5a6b7c8d_worker_geocoding_runtime_boundary.py"
_P2F_REMEDIATION_MIGRATION = "af5b6c7d8e9f_platform_maintenance_control_plane.py"
APP_SECURE_FILES.add(_P2D_MIGRATION)
APP_SECURE_FILES.add(_P2F_REMEDIATION_MIGRATION)


def test_complete_app_secure_ddl_category_allowlist_is_exact() -> None:
    view_contract = {
        "create_view",
        "drop_view",
        "revoke_view",
        "grant_view",
        "comment_view",
    }
    expected = {
        "0022_rbac_phase1_roles_extensions.py": {
            "create_schema",
            "revoke_schema",
            "grant_schema",
            "default_privileges",
            "comment_schema",
            "drop_schema",
        },
        "0025_rbac_p4_bsr_expand.py": view_contract,
        "0027_rbac_p6_perm_snapshots.py": view_contract,
        "0029_rbac_p8_contract.py": view_contract,
        "6f708192a3b4_address_runtime_privilege_boundary.py": {
            "grant_schema",
            "revoke_schema",
        },
        _P2D_MIGRATION: {
            "grant_schema",
            "revoke_schema",
        },
        _P2F_REMEDIATION_MIGRATION: {
            "create_policy",
            "drop_policy",
            "grant_schema",
            "revoke_schema",
        },
        A1.name: view_contract,
    }
    actual = {
        name: _app_secure_ddl_categories(VERSIONS / name)
        for name in APP_SECURE_FILES
    }
    assert actual == expected
