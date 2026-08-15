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
_P2F_DEK_MIGRATION = "b06c7d8e9f0a_tenant_dek_lookup_boundary.py"
_P3A_PROFILE_MIGRATION = "c17d8e9f0a1b_organization_profile_authorization.py"
_P3A_ONBOARDING_MIGRATION = "c27d8e9f0a1c_organization_onboarding_authorization.py"
_P3A_PRINCIPAL_BINDING_MIGRATION = "c37d8e9f0a1d_organization_profile_principal_binding.py"
_P3A_AUTH_DECOUPLING_MIGRATION = "c47d8e9f0a1e_p3a_auth_runtime_decoupling.py"
_P3B_READ_MIGRATION = "c97d8e9f0a23_p3b_registration_read_boundary.py"
_P3B_STORAGE_MIGRATION = "d07d8e9f0a24_p3b_registration_envelope_storage.py"
_P3B_DEK_MIGRATION = "e07d8e9f0a25_p3b_registration_dek_capabilities.py"
_P3B_CREATE_MIGRATION = "f07d8e9f0a26_p3b_registration_create_capability.py"
_P3B_REPLACE_MIGRATION = "g07d8e9f0a27_p3b_registration_replace_capability.py"
_P3B_BACKFILL_MIGRATION = "i07d8e9f0a29_p3b_registration_legacy_backfill_capabilities.py"
_P3B_REPLACE_CORRECTION_MIGRATION = (
    "j07d8e9f0a2a_p3b_registration_replace_without_upsert_reads.py"
)
_P3B_CONTRACT_MIGRATION = "k07d8e9f0a2b_p3b_registration_contract.py"
APP_SECURE_FILES.update(
    {
        _P2D_MIGRATION,
        _P2F_REMEDIATION_MIGRATION,
        _P2F_DEK_MIGRATION,
        _P3A_PROFILE_MIGRATION,
        _P3A_ONBOARDING_MIGRATION,
        _P3A_PRINCIPAL_BINDING_MIGRATION,
        _P3A_AUTH_DECOUPLING_MIGRATION,
        _P3B_READ_MIGRATION,
        _P3B_STORAGE_MIGRATION,
        _P3B_DEK_MIGRATION,
        _P3B_CREATE_MIGRATION,
        _P3B_REPLACE_MIGRATION,
        _P3B_BACKFILL_MIGRATION,
        _P3B_REPLACE_CORRECTION_MIGRATION,
        _P3B_CONTRACT_MIGRATION,
    }
)


def test_complete_app_secure_ddl_category_allowlist_is_exact() -> None:
    view_contract = {
        "create_view",
        "drop_view",
        "revoke_view",
        "grant_view",
        "comment_view",
    }
    view_and_policy_contract = view_contract | {
        "create_policy",
        "drop_policy",
    }
    function_install_contract = {
        "grant_schema",
        "revoke_schema",
    }
    function_install_with_policy_contract = function_install_contract | {
        "create_policy",
        "drop_policy",
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
        "0025_rbac_p4_bsr_expand.py": view_and_policy_contract,
        "0027_rbac_p6_perm_snapshots.py": view_and_policy_contract,
        "0029_rbac_p8_contract.py": view_and_policy_contract,
        "6f708192a3b4_address_runtime_privilege_boundary.py": {
            "create_policy",
            "drop_policy",
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
        # Function-level DDL is covered by the dedicated DEK owner/ACL/runtime
        # contract.  This historical category detector intentionally remains
        # schema/view/policy scoped rather than reclassifying old migrations.
        _P2F_DEK_MIGRATION: set(),
        # P3A's dedicated profile migrations are function/ACL contracts.  C27
        # additionally exposes app_secure schema USAGE to auth_runtime for the
        # bounded onboarding function, and removes it on downgrade.
        _P3A_PROFILE_MIGRATION: set(),
        _P3A_ONBOARDING_MIGRATION: {
            "grant_schema",
            "revoke_schema",
        },
        _P3A_PRINCIPAL_BINDING_MIGRATION: set(),
        _P3A_AUTH_DECOUPLING_MIGRATION: set(),
        # P3B function owners receive schema CREATE only inside installation
        # windows, then lose it immediately. C97/D07 additionally create the two
        # FORCE-RLS tenant policies that begin the registration/storage boundary.
        _P3B_READ_MIGRATION: function_install_with_policy_contract,
        _P3B_STORAGE_MIGRATION: function_install_with_policy_contract,
        _P3B_DEK_MIGRATION: function_install_contract,
        _P3B_CREATE_MIGRATION: function_install_contract,
        _P3B_REPLACE_MIGRATION: function_install_contract,
        _P3B_BACKFILL_MIGRATION: function_install_contract,
        _P3B_REPLACE_CORRECTION_MIGRATION: function_install_contract,
        _P3B_CONTRACT_MIGRATION: function_install_contract,
        A1.name: view_contract,
    }
    actual = {
        name: _app_secure_ddl_categories(VERSIONS / name)
        for name in APP_SECURE_FILES
    }
    assert actual == expected
