from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from app.core.cluster_role_contract import (
    MANIFEST_FILES,
    load_contract_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "app/core/cluster_role_contract.py"
OWNERSHIP_MANIFEST = (
    ROOT
    / "security"
    / "cluster_role_bootstrap"
    / "ownership.v1.json"
)
EXPECTED_OWNERSHIP_SHA256 = (
    "2acfa3881db38549c859cb089682f9c7"
    "98870aa43a010ae2233b68c21ed53855"
)


def test_all_five_machine_readable_manifests_exist() -> None:
    directory = ROOT / "security/cluster_role_bootstrap"

    assert set(MANIFEST_FILES) == {
        "roles",
        "role_settings",
        "memberships",
        "grantors",
        "ownership",
    }

    for filename in MANIFEST_FILES.values():
        path = directory / filename
        assert path.is_file()
        assert path.stat().st_size > 0

    def source_evidence_path_is_portable(value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if value in {".", ".."}:
            return False
        if "/" in value or chr(92) in value:
            return False
        if value != Path(value).name or Path(value).is_absolute():
            return False

        lowered = value.casefold()
        forbidden_fragments = (
            "jeeva" + "shri",
            "doers-hardening-" + "snapshots",
            "cluster-role-bootstrap-" + "planning-v2-",
            "repository-" + "implementation-",
            "final-source-" + "review-",
            "staging-" + "preview-",
            "real-" + "staging-",
            "commit-" + "review-",
            "sandbox:" + "/",
        )
        return not any(fragment in lowered for fragment in forbidden_fragments)

    rejected_source_paths = (
        "/" + "home/" + "jeeva" + "shri/evidence/artifact.json",
        "/" + "Users/" + "jeeva" + "shri/evidence/artifact.json",
        "C:" + chr(92) + "Users" + chr(92) + "jeeva" + "shri" + chr(92) + "artifact.json",
        "/" + "mnt/data/artifact.json",
        "file:" + "///tmp/artifact.json",
        "jeeva" + "shri",
        "doers-hardening-" + "snapshots",
        "cluster-role-bootstrap-" + "planning-v2-123",
        "repository-" + "implementation-123",
        "final-source-" + "review-123",
        "staging-" + "preview-123",
        "real-" + "staging-123",
        "commit-" + "review-123",
        "sandbox:" + "/artifact",
    )

    for rejected_path in rejected_source_paths:
        assert not source_evidence_path_is_portable(rejected_path)

    hexadecimal = set("0123456789abcdef")
    for filename in MANIFEST_FILES.values():
        manifest_path = directory / filename
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_evidence = manifest["source_evidence"]
        assert isinstance(source_evidence, dict)
        assert len(source_evidence) == 7
        for record in source_evidence.values():
            assert set(record) == {"path", "sha256"}
            evidence_path = record["path"]
            evidence_sha256 = record["sha256"]
            assert source_evidence_path_is_portable(evidence_path)
            assert isinstance(evidence_sha256, str)
            assert len(evidence_sha256) == 64
            assert evidence_sha256 == evidence_sha256.lower()
            assert set(evidence_sha256) <= hexadecimal


def test_exact_managed_role_contract() -> None:
    bundle = load_contract_bundle()
    roles = bundle.roles["managed_roles"]

    assert set(roles) == {
        "migration_owner",
        "app_security_owner",
        "app_rls_executor",
        "app_runtime",
        "auth_runtime",
        "audit_writer",
        "readonly_analytics",
        "app_user",
        "branch_admin",
        "branch_viewer",
        "ops_support",
    }

    migration_attributes = roles["migration_owner"]["attributes"]
    assert migration_attributes == {
        "superuser": False,
        "inherit": False,
        "create_role": False,
        "create_db": False,
        "can_login": True,
        "replication": False,
        "bypass_rls": False,
    }
    assert "NOBYPASSRLS" in roles["migration_owner"]["decision"]
    assert "NOINHERIT" in roles["migration_owner"]["decision"]

    assert roles["ops_support"]["attributes"]["bypass_rls"] is False

    for role in (
        "app_security_owner",
        "app_rls_executor",
        "app_runtime",
        "auth_runtime",
        "audit_writer",
        "readonly_analytics",
        "app_user",
        "branch_admin",
        "branch_viewer",
        "ops_support",
    ):
        attributes = roles[role]["attributes"]
        assert attributes == {
            "superuser": False,
            "inherit": False,
            "create_role": False,
            "create_db": False,
            "can_login": False,
            "replication": False,
            "bypass_rls": False,
        }

    assert "pre-tenant" in roles["auth_runtime"]["purpose"]
    assert "must never own schema objects" in roles["auth_runtime"]["decision"]
    assert "must never" in roles["auth_runtime"]["decision"]


def test_role_settings_are_exact() -> None:
    bundle = load_contract_bundle()
    settings = bundle.role_settings["settings_by_role"]

    assert settings["app_runtime"] == {
        "statement_timeout": "5s",
        "lock_timeout": "2s",
        "row_security": "on",
    }
    assert "auth_runtime" in settings

    for role, values in settings.items():
        if role != "app_runtime":
            assert values == {}


def test_exact_membership_and_grantor_contract() -> None:
    bundle = load_contract_bundle()
    rows = bundle.memberships["exact_rows"]

    assert len(rows) == 2
    assert {
        (
            row["granted_role"],
            row["member_role"],
            row["approved_grantor"],
            row["exact_row_count"],
            row["set_option"],
            row["inherit_option"],
            row["admin_option"],
        )
        for row in rows
    } == {
        (
            "app_security_owner",
            "migration_owner",
            "postgres",
            1,
            True,
            False,
            False,
        ),
        (
            "app_rls_executor",
            "migration_owner",
            "postgres",
            1,
            True,
            False,
            False,
        ),
    }

    assert bundle.grantors["approved_membership_grantor"] == "postgres"


def test_auth_runtime_is_not_a_migration_owner_membership() -> None:
    bundle = load_contract_bundle()
    rows = bundle.memberships["exact_rows"]
    assert not any(
        row["granted_role"] == "auth_runtime"
        or row["member_role"] == "auth_runtime"
        for row in rows
    )


def test_ownership_manifest_uses_only_allowed_owners() -> None:
    bundle = load_contract_bundle()
    ownership = bundle.ownership

    assert set(ownership["allowed_target_owners"]) == {
        "migration_owner",
        "app_security_owner",
        "app_rls_executor",
    }
    assert "auth_runtime" not in ownership["allowed_target_owners"]
    assert ownership["objects"]

    for record in ownership["objects"]:
        assert record["target_owner"] in ownership["allowed_target_owners"]


def test_ownership_manifest_matches_reviewed_projection() -> None:
    payload = OWNERSHIP_MANIFEST.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_OWNERSHIP_SHA256

    ownership = json.loads(payload.decode("utf-8"))
    objects = ownership["objects"]
    assert len(objects) == 157
    assert not any(record["object"] == "IF" for record in objects)
    assert {
        "dynamic": False,
        "object": "public",
        "object_type": "TABLE",
        "parent_relation": None,
        "policy": "Preserve source-derived final owner.",
        "target_owner": "migration_owner",
    } not in objects

    identities = [
        (
            record["object_type"],
            record["object"],
            record["parent_relation"],
        )
        for record in objects
    ]
    assert len(identities) == len(set(identities))


def test_cluster_roles_and_memberships_survive_downgrade() -> None:
    bundle = load_contract_bundle()

    assert bundle.roles["downgrade_policy"]["cluster_roles_survive_database_downgrade"] is True
    assert bundle.memberships["downgrade_policy"]["bootstrap_memberships_survive_database_downgrade"] is True
    assert bundle.role_settings["downgrade_policy"]["role_settings_survive_database_downgrade"] is True


def test_validator_module_is_pure_and_read_only() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_import_roots = {
        "asyncpg",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "subprocess",
        "socket",
    }

    imported_roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)

        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "execute",
                    "executemany",
                    "connect",
                    "commit",
                    "rollback",
                }

    assert imported_roots.isdisjoint(forbidden_import_roots)
