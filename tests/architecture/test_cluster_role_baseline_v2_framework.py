from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "docs/architecture/security/cluster-role-baselines/v2"
sys.path.insert(0, str(PACKAGE))

generator = importlib.import_module("generator")
validator = importlib.import_module("validate_manifest")
comparator = importlib.import_module("compare_manifests")

MANAGED = generator.load_json(PACKAGE / "managed_roles.json")
SCHEMA = generator.load_json(PACKAGE / "manifest_schema_v2.json")


def role(name: str, **overrides):
    value = {
        "role_name": name,
        "exists": True,
        "superuser": False,
        "inherit": False,
        "create_role": False,
        "create_database": False,
        "login": False,
        "replication": False,
        "bypass_rls": False,
        "connection_limit": -1,
        "valid_until": "infinity",
        "password_classification": "none",
        "comment": None,
    }
    value.update(overrides)
    return value


def catalog(**overrides):
    value = {
        "postgresql_major_version": 16,
        "roles": [
            role("app_runtime", comment="Runtime"),
            role("migration_owner", login=True, password_classification="scram-sha-256"),
            role("test_runner", login=True, inherit=True, password_classification="scram-sha-256"),
        ],
        "role_settings": [
            {
                "role_name": "app_runtime",
                "database_scope": "*",
                "setting_name": "statement_timeout",
                "setting_value": "5s",
            }
        ],
        "memberships": [
            {
                "granted_role": "app_runtime",
                "member": "test_runner",
                "grantor": "postgres",
                "admin_option": False,
                "inherit_option": True,
                "set_option": True,
            }
        ],
    }
    value.update(overrides)
    return value


def metadata(**overrides):
    value = {
        "captured_at": "2026-07-22T10:00:00Z",
        "execution_mode": "offline_fixture",
        "source_database": "test-primary",
        "label": "r19a-fixture",
    }
    value.update(overrides)
    return value


def manifest(catalog_value=None, metadata_value=None, **kwargs):
    return generator.build_manifest(
        catalog_value or catalog(),
        MANAGED,
        metadata_value or metadata(),
        **kwargs,
    )


def rehash_manifest(value):
    value["state_sha256"] = generator.state_sha256(value)
    return value


def run_capture_output_preflight(output_dir):
    environment = os.environ.copy()
    environment["PYTHON_BIN"] = sys.executable
    environment["PSQL_BIN"] = "/r19a-r4-psql-must-not-run"
    return subprocess.run(
        [
            "bash",
            str(PACKAGE / "capture_peer_admin.sh"),
            "--output-dir",
            str(output_dir),
            "--validate-output-dir-only",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_deterministic_canonicalization_uses_exact_contract():
    value = {"z": 1, "a": {"two": 2, "one": 1}}
    assert generator.canonical_json_bytes(value) == b'{"a":{"one":1,"two":2},"z":1}'


def test_identical_input_produces_identical_bytes_and_hash():
    first = manifest()
    second = manifest()
    assert generator.canonical_json_bytes(first) == generator.canonical_json_bytes(second)
    assert first["state_sha256"] == second["state_sha256"]


def test_dictionary_ordering_does_not_change_hash():
    first = manifest()
    reordered = {key: first["state"][key] for key in reversed(list(first["state"]))}
    assert hashlib.sha256(generator.canonical_json_bytes(reordered)).hexdigest() == first["state_sha256"]


def test_semantic_array_ordering_is_normalized():
    value = catalog(
        roles=list(reversed(catalog()["roles"])),
        memberships=list(reversed(catalog()["memberships"])),
    )
    generated = manifest(value)
    assert [item["role_name"] for item in generated["state"]["roles"]] == sorted(MANAGED_ROLE_NAMES)


def test_capture_metadata_does_not_change_state_hash():
    first = manifest(metadata_value=metadata(captured_at="2026-07-22T10:00:00Z"))
    second = manifest(metadata_value=metadata(captured_at="2026-07-22T11:00:00Z", label="second"))
    assert first["state_sha256"] == second["state_sha256"]


def test_role_attribute_change_changes_hash():
    changed = catalog(roles=[role("app_runtime", inherit=True), *catalog()["roles"][1:]])
    assert manifest()["state_sha256"] != manifest(changed)["state_sha256"]


def test_membership_option_change_changes_hash():
    changed_edge = dict(catalog()["memberships"][0], set_option=False)
    assert manifest()["state_sha256"] != manifest(catalog(memberships=[changed_edge]))["state_sha256"]


def test_database_specific_setting_uses_semantic_name():
    setting = dict(catalog()["role_settings"][0])
    setting.pop("database_scope")
    setting["database_name"] = "physical_db"
    generated = manifest(catalog(role_settings=[setting]), database_scope_map={"physical_db": "primary"})
    assert generated["state"]["role_settings"][0]["database_scope"] == "primary"
    assert "physical_db" not in generator.canonical_json_bytes(generated).decode()


def test_physical_database_map_key_does_not_need_semantic_identifier_syntax():
    setting = dict(catalog()["role_settings"][0])
    setting.pop("database_scope")
    setting["database_name"] = "Physical Database"
    generated = manifest(
        catalog(role_settings=[setting]),
        database_scope_map={"Physical Database": "primary"},
    )
    assert generated["state"]["role_settings"][0]["database_scope"] == "primary"


def test_raw_oid_fields_are_rejected():
    with pytest.raises(generator.ManifestGenerationError, match="OID"):
        manifest(catalog(roles=[dict(role("app_runtime"), role_oid=123)]))


@pytest.mark.parametrize(
    "unsafe",
    [
        "SCRAM-SHA-256$4096:unsafe$verifier",
        "$SCRAM-SHA-256$4096:unsafe$verifier",
        "md5" + "a" * 32,
    ],
)
def test_raw_password_verifier_values_are_rejected(unsafe):
    with pytest.raises(generator.ManifestGenerationError, match="verifier"):
        generator.canonical_json_bytes({"value": unsafe})


@pytest.mark.parametrize("field", ["password", "api_secret", "access_token", "database_url", "raw_verifier"])
def test_secret_looking_fields_are_rejected(field):
    with pytest.raises(generator.ManifestGenerationError, match="Secret-looking"):
        generator.canonical_json_bytes({field: "redacted"})


def test_credential_bearing_urls_are_rejected():
    with pytest.raises(generator.ManifestGenerationError, match="URL"):
        generator.canonical_json_bytes({"value": "postgresql://user:pass@example.invalid/db"})


def test_build_manifest_rejects_top_level_unknown_field():
    with pytest.raises(generator.ManifestGenerationError, match="unknown field"):
        manifest(catalog(unexpected_field="benign"))


def test_build_manifest_rejects_unknown_role_field():
    unexpected = dict(role("app_runtime"), display_color="blue")
    with pytest.raises(generator.ManifestGenerationError, match="unknown field"):
        manifest(catalog(roles=[unexpected]))


def test_build_manifest_rejects_unknown_membership_field():
    unexpected = dict(catalog()["memberships"][0], note="benign")
    with pytest.raises(generator.ManifestGenerationError, match="unknown field"):
        manifest(catalog(memberships=[unexpected]))


def test_build_manifest_rejects_unknown_role_setting_field():
    unexpected = dict(catalog()["role_settings"][0], scope_alias="primary")
    with pytest.raises(generator.ManifestGenerationError, match="unknown field"):
        manifest(catalog(role_settings=[unexpected]))


def test_build_manifest_rejects_unknown_capture_metadata_field():
    with pytest.raises(generator.ManifestGenerationError, match="unknown field"):
        manifest(metadata_value=metadata(operator_name="unapproved"))


def test_build_manifest_rejects_unknown_managed_role_input_field():
    managed = copy.deepcopy(MANAGED)
    managed["unexpected_field"] = "benign"
    with pytest.raises(generator.ManifestGenerationError, match="fields differ"):
        generator.build_manifest(catalog(), managed, metadata())


def test_build_manifest_rejects_missing_required_fields():
    missing_catalog_field = catalog()
    missing_catalog_field.pop("memberships")
    missing_role_field = role("app_runtime")
    missing_role_field.pop("comment")
    missing_membership_field = dict(catalog()["memberships"][0])
    missing_membership_field.pop("grantor")
    missing_setting_field = dict(catalog()["role_settings"][0])
    missing_setting_field.pop("setting_value")
    missing_metadata_field = metadata()
    missing_metadata_field.pop("label")

    invalid_inputs = [
        (missing_catalog_field, metadata()),
        (catalog(roles=[missing_role_field]), metadata()),
        (catalog(memberships=[missing_membership_field]), metadata()),
        (catalog(role_settings=[missing_setting_field]), metadata()),
        (catalog(), missing_metadata_field),
    ]
    for catalog_value, metadata_value in invalid_inputs:
        with pytest.raises(generator.ManifestGenerationError, match="missing required field"):
            generator.build_manifest(
                catalog_value,
                MANAGED,
                metadata_value,
            )


def test_build_manifest_rejects_incorrect_raw_input_types():
    wrong_role_type = role("app_runtime", exists="yes")
    wrong_membership_type = dict(
        catalog()["memberships"][0],
        admin_option="false",
    )
    wrong_setting_type = dict(
        catalog()["role_settings"][0],
        setting_value={"seconds": 5},
    )

    invalid_catalogues = [
        catalog(roles={"not": "an array"}),
        catalog(roles=[wrong_role_type]),
        catalog(memberships=[wrong_membership_type]),
        catalog(role_settings=[wrong_setting_type]),
    ]
    for catalog_value in invalid_catalogues:
        with pytest.raises(generator.ManifestGenerationError):
            generator.build_manifest(catalog_value, MANAGED, metadata())

    with pytest.raises(generator.ManifestGenerationError, match="database_scope_map"):
        generator.build_manifest(
            catalog(),
            MANAGED,
            metadata(),
            database_scope_map=["not", "an", "object"],
        )


def test_build_manifest_rejects_unsupported_setting_scope_alias_combination():
    setting = dict(
        catalog()["role_settings"][0],
        database_name="physical_db",
    )
    with pytest.raises(generator.ManifestGenerationError, match="exactly one"):
        manifest(
            catalog(role_settings=[setting]),
            database_scope_map={"physical_db": "primary"},
        )


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "passwd",
        "rolpassword",
        "password_verifier",
        "verifier",
        "secret",
        "token",
        "access_token",
        "private_key",
        "credential",
        "credential_url",
        "database_url",
    ],
)
def test_build_manifest_rejects_secret_looking_raw_role_fields(field):
    secret_value = "R19A_R2_VALUE_MUST_NOT_BE_ECHOED"
    injected = dict(role("app_runtime"), **{field: secret_value})
    with pytest.raises(generator.ManifestGenerationError) as exc_info:
        manifest(catalog(roles=[injected]))
    assert secret_value not in str(exc_info.value)


def test_build_manifest_rejects_nested_unexpected_secret_field():
    secret_value = "R19A_R2_NESTED_VALUE_MUST_NOT_BE_ECHOED"
    injected = dict(
        role("app_runtime"),
        extension={"context": {"token": secret_value}},
    )
    with pytest.raises(generator.ManifestGenerationError) as exc_info:
        manifest(catalog(roles=[injected]))
    assert secret_value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("unsafe_value", "message"),
    [
        ("SCRAM-SHA-256$4096:unsafe$verifier", "verifier"),
        ("md5" + "a" * 32, "verifier"),
        ("postgresql://user:pass@example.invalid/db", "URL"),
        (
            "-----BEGIN PRIVATE KEY-----\nunsafe\n-----END PRIVATE KEY-----",
            "Private key",
        ),
    ],
)
def test_build_manifest_rejects_secret_bodies_in_allowed_string_fields(
    unsafe_value,
    message,
):
    injected = role("app_runtime", comment=unsafe_value)
    with pytest.raises(generator.ManifestGenerationError, match=message) as exc_info:
        manifest(catalog(roles=[injected]))
    assert unsafe_value not in str(exc_info.value)


def test_build_manifest_safe_error_does_not_echo_rejected_secret():
    secret_value = "R19A_R2_UNIQUE_SECRET_VALUE"
    injected = dict(role("app_runtime"), password=secret_value)
    with pytest.raises(generator.ManifestGenerationError) as exc_info:
        manifest(catalog(roles=[injected]))
    assert secret_value not in str(exc_info.value)
    assert "password" in str(exc_info.value)


def test_build_manifest_returns_no_partial_manifest_or_hash_after_rejection():
    result = None
    injected = dict(role("app_runtime"), password="R19A_R2_REJECTED")
    with pytest.raises(generator.ManifestGenerationError):
        result = manifest(catalog(roles=[injected]))
    assert result is None


def test_build_manifest_allows_harmless_password_prose():
    generated = manifest(
        catalog(
            roles=[
                role(
                    "app_runtime",
                    comment="Password rotation ownership remains unresolved.",
                )
            ]
        )
    )
    app_runtime = next(
        record
        for record in generated["state"]["roles"]
        if record["role_name"] == "app_runtime"
    )
    assert app_runtime["comment"] == "Password rotation ownership remains unresolved."


def test_build_manifest_known_good_catalogue_still_succeeds():
    generated = generator.build_manifest(catalog(), MANAGED, metadata())
    assert generated["state_sha256"] == generator.state_sha256(generated)
    assert generated["state"]["evidence_status"] == "current_evidence"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("Documentation: https://example.invalid/reference", "URL"),
        ("temporary output /tmp/current-manifest.json", "Filesystem path"),
        ("password=[REDACTED]", "Secret assignment"),
    ],
)
def test_embedded_unsafe_text_is_rejected(value, message):
    with pytest.raises(generator.ManifestGenerationError, match=message):
        generator.canonical_json_bytes({"value": value})


def test_secret_looking_role_setting_is_rejected():
    setting = dict(catalog()["role_settings"][0], setting_name="service_token")
    with pytest.raises(generator.ManifestGenerationError, match="Secret-looking role setting"):
        manifest(catalog(role_settings=[setting]))


def test_disposable_source_database_name_is_rejected():
    with pytest.raises(generator.ManifestGenerationError, match="semantic name"):
        manifest(
            metadata_value=metadata(
                source_database="gymflow_platform_billing_test_disposable"
            )
        )


def test_absent_roles_are_explicitly_represented():
    generated = manifest()
    absent = {item["role_name"]: item for item in generated["state"]["roles"]}["app_migrator"]
    assert absent["exists"] is False
    assert absent["password_classification"] is None


def test_valid_until_is_explicit_and_structured():
    generated = manifest(
        catalog_value=catalog(
            roles=[
                role("app_runtime", valid_until={"kind": "unlimited"}),
                role(
                    "migration_owner",
                    login=True,
                    password_classification="scram-sha-256",
                    valid_until={"kind": "timestamp", "value": "2026-08-01T10:30:00+05:30"},
                ),
                role("test_runner", login=True, inherit=True, password_classification="scram-sha-256"),
            ]
        )
    )
    roles = {item["role_name"]: item for item in generated["state"]["roles"]}
    assert roles["app_runtime"]["valid_until"] == {"kind": "unlimited"}
    assert roles["migration_owner"]["valid_until"] == {
        "kind": "timestamp",
        "value": "2026-08-01T05:00:00Z",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        "infinity",
        {},
        {"unexpected": True},
        {"kind": "timestamp"},
        {"kind": "unlimited", "value": "unexpected"},
        {"kind": "timestamp", "value": "2026-08-01T05:00:00Z", "extra": True},
        {"kind": "later"},
        {"kind": "timestamp", "value": 123},
        {"kind": "timestamp", "value": "2026-08-01T10:30:00+05:30"},
    ],
)
def test_validator_rejects_noncanonical_valid_until(invalid):
    generated = manifest()
    app_runtime = next(
        record
        for record in generated["state"]["roles"]
        if record["role_name"] == "app_runtime"
    )
    app_runtime["valid_until"] = invalid
    rehash_manifest(generated)

    with pytest.raises(validator.ManifestValidationError, match="VALID UNTIL|valid_until"):
        validator.validate_manifest(generated, MANAGED, SCHEMA)


@pytest.mark.parametrize(
    "valid",
    [
        {"kind": "unlimited"},
        {"kind": "timestamp", "value": "2026-08-01T05:00:00Z"},
    ],
)
def test_validator_accepts_exact_canonical_valid_until(valid):
    generated = manifest()
    app_runtime = next(
        record
        for record in generated["state"]["roles"]
        if record["role_name"] == "app_runtime"
    )
    app_runtime["valid_until"] = valid
    rehash_manifest(generated)

    assert (
        validator.validate_manifest(generated, MANAGED, SCHEMA)
        == "current_evidence_not_approved"
    )


def test_valid_until_schema_generator_and_validator_remain_aligned():
    valid_until_schema = SCHEMA["$defs"]["valid_until"]
    generated = manifest()
    present_values = [
        record["valid_until"]
        for record in generated["state"]["roles"]
        if record["exists"]
    ]

    assert valid_until_schema["oneOf"]
    assert all(isinstance(value, dict) for value in present_values)
    assert validator.validate_manifest(generated, MANAGED, SCHEMA)


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "statement timeout",
        "statement$timeout",
        "statement_timeout=5s",
        "../statement_timeout",
        "statement/timeout",
    ],
)
def test_validator_rejects_setting_names_outside_schema_pattern(invalid):
    generated = manifest()
    generated["state"]["role_settings"][0]["setting_name"] = invalid
    rehash_manifest(generated)

    with pytest.raises(validator.ManifestValidationError, match="setting"):
        validator.validate_manifest(generated, MANAGED, SCHEMA)


@pytest.mark.parametrize(
    "valid",
    [
        "statement_timeout",
        "idle_in_transaction_session_timeout",
        "app.custom-setting",
    ],
)
def test_validator_accepts_setting_names_matching_schema_and_generator(valid):
    generated = manifest(
        catalog(
            role_settings=[
                dict(catalog()["role_settings"][0], setting_name=valid)
            ]
        )
    )

    assert (
        validator.validate_manifest(generated, MANAGED, SCHEMA)
        == "current_evidence_not_approved"
    )


def test_setting_name_pattern_is_shared_by_schema_generator_and_validator():
    schema_pattern = SCHEMA["$defs"]["role_setting"]["properties"]["setting_name"][
        "pattern"
    ]

    assert schema_pattern == generator._SETTING_NAME.pattern
    validator.validate_schema_document(SCHEMA)


def test_validator_rejects_noncanonical_setting_value():
    generated = manifest()
    generated["state"]["role_settings"][0]["setting_value"] = " 5s "
    rehash_manifest(generated)

    with pytest.raises(validator.ManifestValidationError, match="not canonical"):
        validator.validate_manifest(generated, MANAGED, SCHEMA)


@pytest.mark.parametrize("field", ["granted_role", "member", "grantor"])
def test_validator_rejects_noncanonical_membership_identifiers(field):
    generated = manifest()
    value = generated["state"]["memberships"][0][field]
    generated["state"]["memberships"][0][field] = f" {value} "
    rehash_manifest(generated)

    with pytest.raises(validator.ManifestValidationError, match="not canonical"):
        validator.validate_manifest(generated, MANAGED, SCHEMA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("captured_at", "2026-07-22T15:30:00+05:30"),
        ("source_database", " test-primary "),
        ("label", " r19a-fixture "),
    ],
)
def test_validator_rejects_noncanonical_capture_metadata(field, value):
    generated = manifest()
    generated["capture_metadata"][field] = value

    with pytest.raises(validator.ManifestValidationError, match="not canonical"):
        validator.validate_manifest(generated, MANAGED, SCHEMA)


def test_duplicate_roles_are_rejected():
    duplicate = [role("app_runtime"), role("app_runtime")]
    with pytest.raises(generator.ManifestGenerationError, match="Duplicate role"):
        manifest(catalog(roles=duplicate))


def test_duplicate_membership_edges_are_rejected():
    edge = catalog()["memberships"][0]
    with pytest.raises(generator.ManifestGenerationError, match="Duplicate membership"):
        manifest(catalog(memberships=[edge, dict(edge)]))


def test_schema_mismatch_is_non_comparable():
    left = manifest()
    right = copy.deepcopy(left)
    right["state"]["manifest_schema_version"] = "different/schema"
    result = comparator.compare_manifests(left, right, MANAGED, SCHEMA)
    assert result["comparable"] is False
    assert result["reason_codes"] == ["manifest_schema_version_mismatch"]


def test_generator_mismatch_is_reported_accurately():
    left = manifest()
    right = copy.deepcopy(left)
    right["state"]["generator_version"] = "different/generator"
    result = comparator.compare_manifests(left, right, MANAGED, SCHEMA)
    assert result["reason_codes"] == ["generator_version_mismatch"]


def test_safe_structured_role_membership_and_setting_differences_are_produced():
    left = manifest()
    changed_catalog = catalog(
        roles=[role("app_runtime", comment="Changed"), *catalog()["roles"][1:]],
        role_settings=[dict(catalog()["role_settings"][0], setting_value="6s")],
        memberships=[dict(catalog()["memberships"][0], set_option=False)],
    )
    result = comparator.compare_manifests(left, manifest(changed_catalog), MANAGED, SCHEMA)
    assert result["comparable"] is True
    assert result["equal"] is False
    assert {item["category"] for item in result["differences"]} == {"role", "role_setting", "membership"}


def test_comparator_rejects_raw_string_valid_until_as_non_comparable():
    left = manifest()
    right = copy.deepcopy(left)
    app_runtime = next(
        record
        for record in right["state"]["roles"]
        if record["role_name"] == "app_runtime"
    )
    app_runtime["valid_until"] = "infinity"
    rehash_manifest(right)

    result = comparator.compare_manifests(left, right, MANAGED, SCHEMA)

    assert result["comparable"] is False
    assert result["reason_codes"] == ["manifest_validation_failed"]
    assert result["differences"] == []
    assert "infinity" not in json.dumps(result)


def test_comparator_rejects_invalid_setting_name_as_non_comparable():
    unsafe_name = "INVALID SETTING R19A R4"
    left = manifest()
    right = copy.deepcopy(left)
    right["state"]["role_settings"][0]["setting_name"] = unsafe_name
    rehash_manifest(right)

    result = comparator.compare_manifests(left, right, MANAGED, SCHEMA)

    assert result["comparable"] is False
    assert result["reason_codes"] == ["manifest_validation_failed"]
    assert result["differences"] == []
    assert unsafe_name not in json.dumps(result)


def test_comparator_cli_returns_invalid_input_exit_code(tmp_path):
    unsafe_name = "INVALID SETTING R19A R4 CLI"
    left = manifest()
    right = copy.deepcopy(left)
    right["state"]["role_settings"][0]["setting_name"] = unsafe_name
    rehash_manifest(right)
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    left_path.write_text(json.dumps(left), encoding="utf-8")
    right_path.write_text(json.dumps(right), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            str((PACKAGE / "compare_manifests.py").resolve()),
            str(left_path),
            str(right_path),
            "--format",
            "json",
        ],
        cwd=PACKAGE,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 2
    assert payload["comparable"] is False
    assert payload["reason_codes"] == ["manifest_validation_failed"]
    assert payload["differences"] == []
    assert unsafe_name not in result.stdout
    assert result.stderr == ""


def test_current_evidence_is_not_treated_as_approved():
    assert validator.validate_manifest(manifest(), MANAGED, SCHEMA) == "current_evidence_not_approved"


def test_missing_r19f_approval_evidence_prevents_approval_classification():
    approved = manifest()
    approved["state"]["evidence_status"] = "approved_baseline"
    approved["state_sha256"] = generator.state_sha256(approved)
    with pytest.raises(validator.ManifestValidationError, match="approved policy"):
        validator.approval_classification(approved)


def test_package_sha256sums_verifies():
    result = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=PACKAGE,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_capture_template_is_executable_and_statically_safe():
    capture = PACKAGE / "capture_peer_admin.sh"
    assert capture.exists()
    assert capture.is_file()
    assert capture.stat().st_mode & 0o100
    assert os.access(capture, os.X_OK)

    syntax = subprocess.run(
        ["bash", "-n", str(capture)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stdout + syntax.stderr

    source = capture.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"(?im)^\s*(?:ALTER|CREATE|DROP|GRANT|REVOKE|INSERT|UPDATE|DELETE|TRUNCATE)\s"
    )
    assert forbidden.search(source) is None
    assert "capture_blocked=separate_owner_authorization_required" in source
    assert "baseline_approved=no" in source


def test_capture_template_contains_only_read_only_catalogue_sql():
    source = (PACKAGE / "capture_peer_admin.sh").read_text(encoding="utf-8")
    forbidden = re.compile(r"(?im)^\s*(?:ALTER|CREATE|DROP|GRANT|REVOKE|INSERT|UPDATE|DELETE|TRUNCATE)\s")
    assert forbidden.search(source) is None
    assert "SELECT jsonb_build_object" in source


def test_capture_template_does_not_assume_database_roles():
    source = (PACKAGE / "capture_peer_admin.sh").read_text(encoding="utf-8").upper()
    assert "SET ROLE" not in source
    assert "RESET ROLE" not in source


def test_capture_template_does_not_emit_verifier_values_or_urls():
    source = (PACKAGE / "capture_peer_admin.sh").read_text(encoding="utf-8")
    assert "'rolpassword'" not in source
    assert "SELECT rolpassword" not in source
    assert "render_as_string" not in source
    assert "split_part(role_data.rolpassword, '$', 1) = 'SCRAM-SHA-256'" in source
    assert "baseline_approved=no" in source


@pytest.mark.parametrize(
    "destination",
    [REPOSITORY_ROOT, PACKAGE],
    ids=["repository-root", "package-directory"],
)
def test_capture_output_preflight_rejects_repository_paths_without_mutation(
    destination,
):
    before_mode = destination.stat().st_mode
    before_entries = sorted(path.name for path in destination.iterdir())

    result = run_capture_output_preflight(destination)

    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr == "capture_blocked=unsafe_output_directory\n"
    assert destination.stat().st_mode == before_mode
    assert sorted(path.name for path in destination.iterdir()) == before_entries


def test_capture_output_preflight_does_not_create_repository_descendant():
    destination = REPOSITORY_ROOT / ".r19a-r4-rejected-output-must-not-exist"
    assert not destination.exists()

    result = run_capture_output_preflight(destination)

    assert result.returncode == 3
    assert not destination.exists()


def test_capture_output_preflight_rejects_traversal_into_repository():
    destination = (
        REPOSITORY_ROOT.parent
        / REPOSITORY_ROOT.name
        / ".."
        / REPOSITORY_ROOT.name
        / ".r19a-r4-traversal-must-not-exist"
    )

    result = run_capture_output_preflight(destination)

    assert result.returncode == 3
    assert not destination.resolve(strict=False).exists()


def test_capture_output_preflight_rejects_symlink_into_repository(tmp_path):
    link = tmp_path / "repository-link"
    link.symlink_to(REPOSITORY_ROOT, target_is_directory=True)
    destination = link / ".r19a-r4-symlink-must-not-exist"

    result = run_capture_output_preflight(destination)

    assert result.returncode == 3
    assert not destination.exists()


def test_capture_output_preflight_accepts_similar_external_prefix_without_creation():
    destination = REPOSITORY_ROOT.parent / (
        REPOSITORY_ROOT.name + "-backup-r19a-r4-must-not-exist"
    )
    assert not destination.exists()

    result = run_capture_output_preflight(destination)

    assert result.returncode == 0
    assert result.stdout == "capture_output_path_valid=yes\n"
    assert result.stderr == ""
    assert not destination.exists()


def test_capture_output_validation_precedes_all_destination_mutation():
    source = (PACKAGE / "capture_peer_admin.sh").read_text(encoding="utf-8")
    validation = source.index(
        'OUTPUT_REAL="$(canonicalize_output_dir "$OUTPUT_DIR")"'
    )
    temporary_directory = source.index('TEMP_DIR="$(mktemp')
    mkdir = source.index('mkdir -p -- "$OUTPUT_REAL"')
    chmod = source.index('chmod 700 "$OUTPUT_REAL"')
    install = source.index('install -m 600')

    assert validation < temporary_directory < mkdir < chmod < install


def test_policy_keeps_operational_values_unresolved():
    policy = (PACKAGE / "CLUSTER_ROLE_POLICY_V2.md").read_text(encoding="utf-8")
    assert "STATUS: DRAFT FRAMEWORK" in policy
    assert "OPERATIONAL VALUES: NOT YET APPROVED" in policy
    assert policy.count("OWNER_DECISION_REQUIRED") >= 8
    assert "BASELINE MANIFEST: NOT YET APPROVED" in policy


def test_schema_rejects_disposable_database_names_and_unsafe_free_text():
    database_scope = SCHEMA["$defs"]["database_scope"]
    secret_free_text = SCHEMA["$defs"]["secret_free_text"]
    assert database_scope["not"]["pattern"] == "^gymflow_platform_billing_test_"
    assert secret_free_text["not"]["anyOf"]


MANAGED_ROLE_NAMES = [record["role_name"] for record in MANAGED["roles"]]
