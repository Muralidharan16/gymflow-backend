#!/usr/bin/env python3
"""Fail-closed offline validation for cluster-role manifest v2."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import generator


SCHEMA_DOCUMENT_ID = "https://doers.internal/schemas/cluster-role-manifest-v2.json"
TOP_LEVEL_FIELDS = {"state", "state_sha256", "capture_metadata"}
STATE_FIELDS = {
    "manifest_schema_version",
    "generator_version",
    "policy_version",
    "policy_status",
    "evidence_status",
    "postgresql_major_version",
    "relevant_role_set_id",
    "relevant_role_set_sha256",
    "relevant_roles",
    "roles",
    "role_settings",
    "memberships",
}
ROLE_FIELDS = {
    "role_name",
    "exists",
    "superuser",
    "inherit",
    "create_role",
    "create_database",
    "login",
    "replication",
    "bypass_rls",
    "connection_limit",
    "valid_until",
    "password_classification",
    "comment",
}
SETTING_FIELDS = {"role_name", "database_scope", "setting_name", "setting_value"}
MEMBERSHIP_FIELDS = {
    "granted_role",
    "member",
    "grantor",
    "admin_option",
    "inherit_option",
    "set_option",
}
METADATA_FIELDS = {"captured_at", "execution_mode", "source_database", "label"}


class ManifestValidationError(ValueError):
    """Raised when a manifest fails the R19A security contract."""


def validate_schema_document(schema: Mapping[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ManifestValidationError("Manifest schema must use JSON Schema 2020-12")
    if schema.get("$id") != SCHEMA_DOCUMENT_ID:
        raise ManifestValidationError("Manifest schema document identity is invalid")
    if schema.get("additionalProperties") is not False:
        raise ManifestValidationError("Manifest schema must reject unknown top-level fields")
    if not isinstance(schema.get("$defs"), Mapping):
        raise ManifestValidationError("Manifest schema definitions are missing")
    _setting_name_pattern(schema)


def validate_manifest(
    manifest: Mapping[str, Any],
    managed_roles: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    approval_evidence: Mapping[str, Any] | None = None,
) -> str:
    validate_schema_document(schema)
    if set(manifest) != TOP_LEVEL_FIELDS:
        raise ManifestValidationError("Manifest top-level fields differ from the v2 contract")
    generator.ensure_secret_free(manifest)
    state = _required_mapping(manifest.get("state"), "state")
    metadata = _required_mapping(manifest.get("capture_metadata"), "capture_metadata")
    if set(state) != STATE_FIELDS:
        raise ManifestValidationError("Manifest state fields differ from the v2 contract")
    if set(metadata) != METADATA_FIELDS:
        raise ManifestValidationError("Capture metadata fields differ from the v2 contract")

    role_names = generator.validate_managed_roles(managed_roles)
    _expect(state, "manifest_schema_version", generator.SCHEMA_ID)
    _expect(state, "generator_version", generator.GENERATOR_ID)
    _expect(state, "policy_version", generator.POLICY_ID)
    _expect(state, "policy_status", generator.POLICY_STATUS)
    _expect(state, "relevant_role_set_id", managed_roles["relevant_role_set_id"])
    _expect(state, "relevant_role_set_sha256", managed_roles["relevant_role_set_sha256"])
    if state.get("relevant_roles") != role_names:
        raise ManifestValidationError("Manifest relevant roles differ from the frozen role set")

    _validate_roles(state.get("roles"), role_names)
    _validate_settings(
        state.get("role_settings"),
        role_names,
        _setting_name_pattern(schema),
    )
    _validate_memberships(state.get("memberships"), role_names)
    generator._validated_major_version(state.get("postgresql_major_version"))
    normalized_metadata = generator._normalize_metadata(metadata)
    if normalized_metadata != dict(metadata):
        raise ManifestValidationError("Capture metadata is not canonical")

    declared_hash = manifest.get("state_sha256")
    if not isinstance(declared_hash, str) or declared_hash != generator.state_sha256(manifest):
        raise ManifestValidationError("Declared state hash does not match canonical state")

    return approval_classification(manifest, approval_evidence=approval_evidence)


def approval_classification(
    manifest: Mapping[str, Any],
    *,
    approval_evidence: Mapping[str, Any] | None = None,
) -> str:
    state = _required_mapping(manifest.get("state"), "state")
    evidence_status = state.get("evidence_status")
    if evidence_status in {"current_evidence", "candidate"}:
        if approval_evidence:
            raise ManifestValidationError("Non-approved evidence must not carry approval artifacts")
        return "current_evidence_not_approved"
    if evidence_status != "approved_baseline":
        raise ManifestValidationError("Unknown evidence status")
    if state.get("policy_status") != "approved":
        raise ManifestValidationError("Approved baseline requires an approved policy")
    required = {
        "manifest_sha256",
        "approval_record_sha256",
        "explicit_owner_approval",
    }
    if not approval_evidence or set(approval_evidence) != required:
        raise ManifestValidationError("R19F approval evidence is required")
    if approval_evidence.get("explicit_owner_approval") is not True:
        raise ManifestValidationError("Explicit owner approval is required")
    for field in ("manifest_sha256", "approval_record_sha256"):
        value = approval_evidence.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ManifestValidationError(f"Invalid approval evidence field: {field}")
    return "approved_baseline"


def _validate_roles(value: Any, role_names: Sequence[str]) -> None:
    if not isinstance(value, list) or len(value) != len(role_names):
        raise ManifestValidationError("Manifest must contain one explicit record per managed role")
    observed: list[str] = []
    for record in value:
        item = _required_mapping(record, "role")
        if set(item) != ROLE_FIELDS:
            raise ManifestValidationError("Role fields differ from the v2 contract")
        name = item.get("role_name")
        if not isinstance(name, str):
            raise ManifestValidationError("Role name must be a string")
        observed.append(name)
        exists = item.get("exists")
        if not isinstance(exists, bool):
            raise ManifestValidationError(f"Role existence must be boolean for {name}")
        if not exists:
            for field in ROLE_FIELDS - {"role_name", "exists"}:
                if item.get(field) is not None:
                    raise ManifestValidationError(f"Absent role {name} has populated field {field}")
            continue
        for field in {
            "superuser",
            "inherit",
            "create_role",
            "create_database",
            "login",
            "replication",
            "bypass_rls",
        }:
            if not isinstance(item.get(field), bool):
                raise ManifestValidationError(f"Role field {field} must be boolean for {name}")
        generator._connection_limit(item.get("connection_limit"), name)
        _validate_canonical_valid_until(item.get("valid_until"))
        if item.get("password_classification") not in generator.ALLOWED_PASSWORD_CLASSIFICATIONS:
            raise ManifestValidationError(f"Invalid password classification for {name}")
        generator._nullable_string(item.get("comment"), f"comment for {name}")
    if observed != list(role_names) or len(observed) != len(set(observed)):
        raise ManifestValidationError("Role records must be unique and semantically ordered")


def _validate_settings(
    value: Any,
    role_names: Sequence[str],
    setting_name_pattern: re.Pattern[str],
) -> None:
    if not isinstance(value, list):
        raise ManifestValidationError("Role settings must be an array")
    identities: set[tuple[str, str, str]] = set()
    sort_keys: list[tuple[str, str, str, str]] = []
    for record in value:
        item = _required_mapping(record, "role_setting")
        if set(item) != SETTING_FIELDS:
            raise ManifestValidationError("Role-setting fields differ from the v2 contract")
        role_name = item.get("role_name")
        if role_name not in role_names:
            raise ManifestValidationError("Setting belongs to an unmanaged role")
        scope = item.get("database_scope")
        if not isinstance(scope, str) or not generator._SEMANTIC_DATABASE.fullmatch(scope):
            raise ManifestValidationError("Role setting has a non-semantic database scope")
        if scope != "*" and (scope.isdigit() or scope.startswith("gymflow_platform_billing_test_")):
            raise ManifestValidationError("Role setting contains an OID or temporary database name")
        raw_setting_name = item.get("setting_name")
        if (
            not isinstance(raw_setting_name, str)
            or not raw_setting_name
            or raw_setting_name != raw_setting_name.strip()
        ):
            raise ManifestValidationError("Role-setting name violates the schema contract")
        setting_name = raw_setting_name
        if not setting_name_pattern.fullmatch(setting_name):
            raise ManifestValidationError("Role-setting name violates the schema contract")
        if any(
            part in setting_name.lower()
            for part in generator._PROHIBITED_KEY_PARTS
        ):
            raise ManifestValidationError("Secret-looking role-setting name is forbidden")
        raw_setting_value = item.get("setting_value")
        setting_value = generator._required_string(raw_setting_value, "setting_value")
        if raw_setting_value != setting_value:
            raise ManifestValidationError("Role-setting value is not canonical")
        identity = (role_name, scope, setting_name)
        if identity in identities:
            raise ManifestValidationError("Duplicate role setting")
        identities.add(identity)
        sort_keys.append((role_name, scope, setting_name, setting_value))
    if sort_keys != sorted(sort_keys):
        raise ManifestValidationError("Role settings are not semantically ordered")


def _validate_memberships(value: Any, role_names: Sequence[str]) -> None:
    if not isinstance(value, list):
        raise ManifestValidationError("Memberships must be an array")
    edges: set[tuple[str, str]] = set()
    sort_keys: list[tuple[Any, ...]] = []
    for record in value:
        item = _required_mapping(record, "membership")
        if set(item) != MEMBERSHIP_FIELDS:
            raise ManifestValidationError("Membership fields differ from the v2 contract")
        granted = generator._validated_role_name(item.get("granted_role"))
        member = generator._validated_role_name(item.get("member"))
        grantor = generator._validated_role_name(item.get("grantor"))
        if (
            item.get("granted_role") != granted
            or item.get("member") != member
            or item.get("grantor") != grantor
        ):
            raise ManifestValidationError("Membership role identifiers are not canonical")
        if granted not in role_names and member not in role_names:
            raise ManifestValidationError("Membership is unrelated to the managed role set")
        edge = (granted, member)
        if edge in edges:
            raise ManifestValidationError("Duplicate membership edge")
        edges.add(edge)
        options: list[bool] = []
        for field in ("admin_option", "inherit_option", "set_option"):
            option = item.get(field)
            if not isinstance(option, bool):
                raise ManifestValidationError(f"Membership option {field} must be boolean")
            options.append(option)
        sort_keys.append((granted, member, grantor, *options))
    if sort_keys != sorted(sort_keys):
        raise ManifestValidationError("Memberships are not semantically ordered")


def _validate_canonical_valid_until(value: Any) -> None:
    item = _required_mapping(value, "valid_until")
    fields = set(item)
    kind = item.get("kind")
    if fields == {"kind"} and kind == "unlimited":
        return
    if fields != {"kind", "value"} or kind != "timestamp":
        raise ManifestValidationError("Role VALID UNTIL violates the canonical schema")
    timestamp = item.get("value")
    if not isinstance(timestamp, str):
        raise ManifestValidationError("Role VALID UNTIL timestamp must be a string")
    try:
        normalized = generator._normalize_timestamp(timestamp, "valid_until")
    except generator.ManifestGenerationError as exc:
        raise ManifestValidationError("Role VALID UNTIL timestamp is invalid") from exc
    if normalized != timestamp:
        raise ManifestValidationError("Role VALID UNTIL timestamp is not canonical UTC")


def _setting_name_pattern(schema: Mapping[str, Any]) -> re.Pattern[str]:
    try:
        pattern = schema["$defs"]["role_setting"]["properties"]["setting_name"]["pattern"]
    except (KeyError, TypeError) as exc:
        raise ManifestValidationError("Manifest schema setting-name contract is missing") from exc
    if not isinstance(pattern, str) or pattern != generator._SETTING_NAME.pattern:
        raise ManifestValidationError(
            "Manifest schema setting-name contract differs from the generator"
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ManifestValidationError("Manifest schema setting-name pattern is invalid") from exc


def _expect(state: Mapping[str, Any], field: str, expected: Any) -> None:
    if state.get(field) != expected:
        raise ManifestValidationError(f"Unexpected {field}")


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{field} must be an object")
    return value


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--managed-roles", default=str(base / "managed_roles.json"))
    parser.add_argument("--schema", default=str(base / "manifest_schema_v2.json"))
    parser.add_argument("--format", choices=("human", "json"), default="human")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        classification = validate_manifest(
            generator.load_json(args.manifest),
            generator.load_json(args.managed_roles),
            generator.load_json(args.schema),
        )
    except (ManifestValidationError, generator.ManifestGenerationError, OSError, json.JSONDecodeError) as exc:
        if args.format == "json":
            print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"manifest_valid=no error={exc}")
        return 2
    result = {"valid": True, "approval_classification": classification}
    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"manifest_valid=yes approval_classification={classification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
