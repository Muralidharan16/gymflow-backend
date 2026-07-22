#!/usr/bin/env python3
"""Deterministic, secret-free cluster-role manifest generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_ID = "doers-cluster-role-manifest/v2"
GENERATOR_ID = "doers-role-manifest-generator/2.0.0"
POLICY_ID = "doers-cluster-role-policy/v2-draft-r19a"
POLICY_STATUS = "draft_framework"
CURRENT_EVIDENCE = "current_evidence"
ALLOWED_PASSWORD_CLASSIFICATIONS = {
    "none",
    "scram-sha-256",
    "md5",
    "unrecognized",
}

_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SETTING_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SEMANTIC_DATABASE = re.compile(r"^(?:\*|[a-z][a-z0-9_-]{0,62})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RAW_SCRAM = re.compile(r"(?<![A-Za-z0-9])\$?SCRAM-SHA-256\$", re.IGNORECASE)
_RAW_MD5 = re.compile(r"(?<![A-Za-z0-9])md5[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)
_CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE)
_ANY_URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PATH_VALUE = re.compile(r"(?:^|[\s(])(?:/(?:[^\s/]+/?)+|[A-Za-z]:[\\/][^\s]+)")
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:credential|password|private_key|secret|token|verifier)\s*[:=]",
    re.IGNORECASE,
)
_PROHIBITED_KEY_PARTS = {
    "credential",
    "dsn",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
    "url",
    "verifier",
}
_ALLOWED_SECURITY_KEYS = {"password_classification"}
_ALLOWED_ROLE_CLASSIFICATIONS = {
    "legacy_production_capability",
    "production_capability",
    "production_migration_identity",
    "production_ownership_role",
    "test_identity",
}
_RAW_CATALOG_FIELDS = {
    "postgresql_major_version",
    "roles",
    "role_settings",
    "memberships",
}
_RAW_ROLE_FIELDS = {
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
_RAW_ROLE_STATE_FIELDS = _RAW_ROLE_FIELDS - {"role_name", "exists"}
_RAW_MEMBERSHIP_FIELDS = {
    "granted_role",
    "member",
    "grantor",
    "admin_option",
    "inherit_option",
    "set_option",
}
_RAW_SETTING_COMMON_FIELDS = {
    "role_name",
    "setting_name",
    "setting_value",
}
_RAW_SETTING_SCOPE_FIELDS = {"database_scope", "database_name"}
_RAW_METADATA_FIELDS = {
    "captured_at",
    "execution_mode",
    "source_database",
    "label",
}


class ManifestGenerationError(ValueError):
    """Raised when input cannot produce a safe deterministic manifest."""


def canonical_json_bytes(value: Any) -> bytes:
    ensure_secret_free(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def role_set_sha256(role_names: Sequence[str]) -> str:
    ordered = list(role_names)
    if ordered != sorted(ordered) or len(ordered) != len(set(ordered)):
        raise ManifestGenerationError("Managed role names must be unique and ordered")
    return sha256_hex(canonical_json_bytes(ordered))


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.write_bytes(canonical_json_bytes(value) + b"\n")


def ensure_secret_free(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ManifestGenerationError(f"Non-string key at {path}")
            lowered = key.lower()
            if key not in _ALLOWED_SECURITY_KEYS and any(part in lowered for part in _PROHIBITED_KEY_PARTS):
                raise ManifestGenerationError(f"Secret-looking field is forbidden at {path}.{key}")
            if "oid" in lowered:
                raise ManifestGenerationError(f"OID-bearing field is forbidden at {path}.{key}")
            ensure_secret_free(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            ensure_secret_free(nested, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _RAW_SCRAM.search(value) or _RAW_MD5.search(value):
        raise ManifestGenerationError(f"Raw password verifier is forbidden at {path}")
    if _CREDENTIAL_URL.search(value) or _ANY_URL.search(value):
        raise ManifestGenerationError(f"URL is forbidden at {path}")
    if _PRIVATE_KEY.search(value):
        raise ManifestGenerationError(f"Private key material is forbidden at {path}")
    if _PATH_VALUE.search(value):
        raise ManifestGenerationError(f"Filesystem path is forbidden at {path}")
    if _SECRET_ASSIGNMENT.search(value):
        raise ManifestGenerationError(f"Secret assignment is forbidden at {path}")


def validate_raw_build_inputs(
    catalog: Mapping[str, Any],
    managed_roles: Mapping[str, Any],
    capture_metadata: Mapping[str, Any],
    database_scope_map: Mapping[str, str] | None = None,
) -> None:
    catalog_record = _required_input_mapping(catalog, "catalog")
    managed_record = _required_input_mapping(managed_roles, "managed_roles")
    metadata_record = _required_input_mapping(capture_metadata, "capture_metadata")
    scope_map = (
        {}
        if database_scope_map is None
        else _required_input_mapping(database_scope_map, "database_scope_map")
    )

    ensure_secret_free(catalog_record, "$.catalog")
    ensure_secret_free(managed_record, "$.managed_roles")
    ensure_secret_free(metadata_record, "$.capture_metadata")
    ensure_secret_free(scope_map, "$.database_scope_map")

    _require_exact_input_fields(catalog_record, _RAW_CATALOG_FIELDS, "catalog")
    _validated_major_version(catalog_record["postgresql_major_version"])
    _validate_database_scope_map(scope_map)
    _validate_raw_roles(catalog_record["roles"])
    _validate_raw_settings(catalog_record["role_settings"], scope_map)
    _validate_raw_memberships(catalog_record["memberships"])
    _validate_raw_metadata(metadata_record)


def _required_input_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestGenerationError(f"Raw input {path} rejected: expected object")
    return value


def _require_exact_input_fields(
    record: Mapping[str, Any],
    allowed: set[str],
    path: str,
) -> None:
    observed = set(record)
    unknown = sorted(observed - allowed)
    missing = sorted(allowed - observed)
    if unknown:
        raise ManifestGenerationError(
            f"Raw input {path} rejected: unknown field {unknown[0]}"
        )
    if missing:
        raise ManifestGenerationError(
            f"Raw input {path} rejected: missing required field {missing[0]}"
        )


def _validate_raw_roles(value: Any) -> None:
    if not isinstance(value, list):
        raise ManifestGenerationError("Raw input catalog.roles rejected: expected array")
    observed: set[str] = set()
    for index, raw_record in enumerate(value):
        path = f"catalog.roles[{index}]"
        record = _required_input_mapping(raw_record, path)
        _require_exact_input_fields(record, _RAW_ROLE_FIELDS, path)
        name = _validated_role_name(record["role_name"])
        if name in observed:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: Duplicate role identifier"
            )
        observed.add(name)
        exists = record["exists"]
        if not isinstance(exists, bool):
            raise ManifestGenerationError(
                f"Raw input {path}.exists rejected: expected boolean"
            )
        if not exists:
            for field in sorted(_RAW_ROLE_STATE_FIELDS):
                if record[field] is not None:
                    raise ManifestGenerationError(
                        f"Raw input {path}.{field} rejected: absent-role field must be null"
                    )
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
            _required_bool(record, field, name)
        _connection_limit(record["connection_limit"], name)
        _normalize_valid_until(record["valid_until"], name)
        password_classification = record["password_classification"]
        if (
            not isinstance(password_classification, str)
            or password_classification not in ALLOWED_PASSWORD_CLASSIFICATIONS
        ):
            raise ManifestGenerationError(
                f"Raw input {path}.password_classification rejected: invalid classification"
            )
        _nullable_string(record["comment"], f"comment for {name}")


def _validate_raw_settings(
    value: Any,
    database_scope_map: Mapping[str, str],
) -> None:
    if not isinstance(value, list):
        raise ManifestGenerationError(
            "Raw input catalog.role_settings rejected: expected array"
        )
    observed: set[tuple[str, str, str]] = set()
    used_database_names: set[str] = set()
    allowed = _RAW_SETTING_COMMON_FIELDS | _RAW_SETTING_SCOPE_FIELDS
    for index, raw_record in enumerate(value):
        path = f"catalog.role_settings[{index}]"
        record = _required_input_mapping(raw_record, path)
        unknown = sorted(set(record) - allowed)
        if unknown:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: unknown field {unknown[0]}"
            )
        missing = sorted(_RAW_SETTING_COMMON_FIELDS - set(record))
        if missing:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: missing required field {missing[0]}"
            )
        scope_fields = sorted(set(record) & _RAW_SETTING_SCOPE_FIELDS)
        if len(scope_fields) != 1:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: exactly one database scope field is required"
            )
        expected = _RAW_SETTING_COMMON_FIELDS | {scope_fields[0]}
        _require_exact_input_fields(record, expected, path)

        role_name = _validated_role_name(record["role_name"])
        setting_name = _required_string(record["setting_name"], "setting_name")
        if not _SETTING_NAME.fullmatch(setting_name):
            raise ManifestGenerationError(
                f"Raw input {path}.setting_name rejected: invalid setting name"
            )
        if any(part in setting_name.lower() for part in _PROHIBITED_KEY_PARTS):
            raise ManifestGenerationError(
                f"Raw input {path}.setting_name rejected: Secret-looking role setting"
            )
        _required_string(record["setting_value"], "setting_value")
        raw_scope = record[scope_fields[0]]
        if scope_fields[0] == "database_scope" and not isinstance(raw_scope, str):
            raise ManifestGenerationError(
                f"Raw input {path}.database_scope rejected: expected string"
            )
        if scope_fields[0] == "database_name" and not (
            raw_scope is None or isinstance(raw_scope, str)
        ):
            raise ManifestGenerationError(
                f"Raw input {path}.database_name rejected: expected string or null"
            )
        scope = _semantic_database_scope(record, database_scope_map)
        if scope_fields[0] == "database_name":
            database_name = record["database_name"]
            if database_name not in {None, "", "*"}:
                used_database_names.add(database_name)
        identity = (role_name, scope, setting_name)
        if identity in observed:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: duplicate semantic role setting"
            )
        observed.add(identity)
    unused_database_names = sorted(set(database_scope_map) - used_database_names)
    if unused_database_names:
        raise ManifestGenerationError(
            "Raw input database_scope_map rejected: unused source key"
        )


def _validate_raw_memberships(value: Any) -> None:
    if not isinstance(value, list):
        raise ManifestGenerationError(
            "Raw input catalog.memberships rejected: expected array"
        )
    observed: set[tuple[str, str]] = set()
    for index, raw_record in enumerate(value):
        path = f"catalog.memberships[{index}]"
        record = _required_input_mapping(raw_record, path)
        _require_exact_input_fields(record, _RAW_MEMBERSHIP_FIELDS, path)
        granted_role = _validated_role_name(record["granted_role"])
        member = _validated_role_name(record["member"])
        _validated_role_name(record["grantor"])
        for field in ("admin_option", "inherit_option", "set_option"):
            if not isinstance(record[field], bool):
                raise ManifestGenerationError(
                    f"Raw input {path}.{field} rejected: expected boolean"
                )
        identity = (granted_role, member)
        if identity in observed:
            raise ManifestGenerationError(
                f"Raw input {path} rejected: Duplicate membership edge"
            )
        observed.add(identity)


def _validate_raw_metadata(metadata: Mapping[str, Any]) -> None:
    path = "capture_metadata"
    _require_exact_input_fields(metadata, _RAW_METADATA_FIELDS, path)
    _normalize_timestamp(metadata["captured_at"], "captured_at")
    execution_mode = metadata["execution_mode"]
    if not isinstance(execution_mode, str) or execution_mode not in {
        "offline_fixture",
        "peer_admin_read_only",
    }:
        raise ManifestGenerationError(
            "Raw input capture_metadata.execution_mode rejected: invalid mode"
        )
    source_database = _required_string(
        metadata["source_database"],
        "source_database",
    )
    if (
        not _SEMANTIC_DATABASE.fullmatch(source_database)
        or source_database == "*"
        or source_database.startswith("gymflow_platform_billing_test_")
    ):
        raise ManifestGenerationError(
            "Raw input capture_metadata.source_database rejected: invalid semantic name"
        )
    label = _required_string(metadata["label"], "label")
    if len(label) > 160:
        raise ManifestGenerationError(
            "Raw input capture_metadata.label rejected: value is too long"
        )


def _validate_database_scope_map(value: Mapping[str, Any]) -> None:
    for actual_name, semantic_name in value.items():
        if isinstance(actual_name, str):
            ensure_secret_free(
                actual_name,
                "$.database_scope_map.<source_key>",
            )
        if (
            not isinstance(actual_name, str)
            or not actual_name.strip()
            or actual_name == "*"
        ):
            raise ManifestGenerationError(
                "Raw input database_scope_map rejected: invalid source key"
            )
        if (
            not isinstance(semantic_name, str)
            or semantic_name == "*"
            or not _SEMANTIC_DATABASE.fullmatch(semantic_name)
            or semantic_name.startswith("gymflow_platform_billing_test_")
        ):
            raise ManifestGenerationError(
                "Raw input database_scope_map rejected: invalid semantic value"
            )


def validate_managed_roles(document: Mapping[str, Any]) -> list[str]:
    expected_keys = {
        "relevant_role_set_id",
        "relevant_role_set_sha256",
        "checksum_contract",
        "roles",
    }
    if set(document) != expected_keys:
        raise ManifestGenerationError("Managed-role document fields differ from the v2 contract")
    if _required_string(document.get("relevant_role_set_id"), "relevant_role_set_id") != (
        "doers-managed-cluster-roles/r4r17-v1"
    ):
        raise ManifestGenerationError("Managed-role set identity is invalid")
    if document.get("checksum_contract") != "sha256(canonical-json(ordered-role-names))":
        raise ManifestGenerationError("Managed-role checksum contract is invalid")
    roles = document.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ManifestGenerationError("Managed-role document must contain roles")
    names: list[str] = []
    for record in roles:
        if not isinstance(record, Mapping):
            raise ManifestGenerationError("Managed-role records must be objects")
        if set(record) != {
            "role_name",
            "classification",
            "expected_current_presence",
            "inclusion_reason",
        }:
            raise ManifestGenerationError("Managed-role record fields differ from the contract")
        name = _validated_role_name(record["role_name"])
        if record["classification"] not in _ALLOWED_ROLE_CLASSIFICATIONS:
            raise ManifestGenerationError(f"Invalid managed-role classification for {name}")
        if record["expected_current_presence"] not in {"present", "absent"}:
            raise ManifestGenerationError(f"Invalid expected presence for {name}")
        _required_string(record["inclusion_reason"], f"inclusion_reason for {name}")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ManifestGenerationError("Managed roles must be unique and ordered")
    declared = document.get("relevant_role_set_sha256")
    if declared != role_set_sha256(names):
        raise ManifestGenerationError("Managed-role checksum does not match ordered role names")
    ensure_secret_free(document)
    return names


def build_manifest(
    catalog: Mapping[str, Any],
    managed_roles: Mapping[str, Any],
    capture_metadata: Mapping[str, Any],
    *,
    database_scope_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    validate_raw_build_inputs(
        catalog,
        managed_roles,
        capture_metadata,
        database_scope_map,
    )
    role_names = validate_managed_roles(managed_roles)
    state = {
        "manifest_schema_version": SCHEMA_ID,
        "generator_version": GENERATOR_ID,
        "policy_version": POLICY_ID,
        "policy_status": POLICY_STATUS,
        "evidence_status": CURRENT_EVIDENCE,
        "postgresql_major_version": _validated_major_version(catalog.get("postgresql_major_version")),
        "relevant_role_set_id": managed_roles["relevant_role_set_id"],
        "relevant_role_set_sha256": managed_roles["relevant_role_set_sha256"],
        "relevant_roles": role_names,
        "roles": _normalize_roles(catalog.get("roles", []), role_names),
        "role_settings": _normalize_settings(
            catalog.get("role_settings", []),
            role_names,
            database_scope_map or {},
        ),
        "memberships": _normalize_memberships(catalog.get("memberships", []), role_names),
    }
    metadata = _normalize_metadata(capture_metadata)
    ensure_secret_free(state)
    ensure_secret_free(metadata)
    return {
        "state": state,
        "state_sha256": sha256_hex(canonical_json_bytes(state)),
        "capture_metadata": metadata,
    }


def state_sha256(manifest: Mapping[str, Any]) -> str:
    state = manifest.get("state")
    if not isinstance(state, Mapping):
        raise ManifestGenerationError("Manifest state must be an object")
    return sha256_hex(canonical_json_bytes(state))


def _normalize_roles(records: Any, role_names: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ManifestGenerationError("Catalogue roles must be an array")
    by_name: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ManifestGenerationError("Catalogue role records must be objects")
        _reject_oid_fields(record)
        name = _validated_role_name(record.get("role_name"))
        if name not in role_names:
            raise ManifestGenerationError(f"Unexpected role record: {name}")
        if name in by_name:
            raise ManifestGenerationError(f"Duplicate role record: {name}")
        by_name[name] = record

    normalized: list[dict[str, Any]] = []
    for name in role_names:
        record = by_name.get(name)
        if record is None or not bool(record.get("exists", True)):
            normalized.append(_absent_role(name))
            continue
        password_classification = record.get("password_classification")
        if password_classification not in ALLOWED_PASSWORD_CLASSIFICATIONS:
            raise ManifestGenerationError(f"Invalid password classification for {name}")
        normalized.append(
            {
                "role_name": name,
                "exists": True,
                "superuser": _required_bool(record, "superuser", name),
                "inherit": _required_bool(record, "inherit", name),
                "create_role": _required_bool(record, "create_role", name),
                "create_database": _required_bool(record, "create_database", name),
                "login": _required_bool(record, "login", name),
                "replication": _required_bool(record, "replication", name),
                "bypass_rls": _required_bool(record, "bypass_rls", name),
                "connection_limit": _connection_limit(record.get("connection_limit"), name),
                "valid_until": _normalize_valid_until(record.get("valid_until"), name),
                "password_classification": password_classification,
                "comment": _nullable_string(record.get("comment"), f"comment for {name}"),
            }
        )
    return normalized


def _normalize_settings(
    records: Any,
    role_names: Sequence[str],
    database_scope_map: Mapping[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ManifestGenerationError("Catalogue role settings must be an array")
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ManifestGenerationError("Role settings must be objects")
        _reject_oid_fields(record)
        role_name = _validated_role_name(record.get("role_name"))
        if role_name not in role_names:
            raise ManifestGenerationError(f"Setting belongs to unmanaged role: {role_name}")
        database_scope = _semantic_database_scope(record, database_scope_map)
        setting_name = _required_string(record.get("setting_name"), "setting_name")
        if not _SETTING_NAME.fullmatch(setting_name):
            raise ManifestGenerationError(f"Invalid role setting name: {setting_name}")
        if any(part in setting_name.lower() for part in _PROHIBITED_KEY_PARTS):
            raise ManifestGenerationError(f"Secret-looking role setting is forbidden: {setting_name}")
        setting_value = _required_string(record.get("setting_value"), "setting_value")
        identity = (role_name, database_scope, setting_name)
        if identity in identities:
            raise ManifestGenerationError(f"Duplicate role setting: {identity}")
        identities.add(identity)
        normalized.append(
            {
                "role_name": role_name,
                "database_scope": database_scope,
                "setting_name": setting_name,
                "setting_value": setting_value,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["role_name"],
            item["database_scope"],
            item["setting_name"],
            item["setting_value"],
        ),
    )


def _normalize_memberships(records: Any, role_names: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ManifestGenerationError("Catalogue memberships must be an array")
    normalized: list[dict[str, Any]] = []
    edges: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ManifestGenerationError("Membership records must be objects")
        _reject_oid_fields(record)
        granted_role = _validated_role_name(record.get("granted_role"))
        member = _validated_role_name(record.get("member"))
        grantor = _validated_role_name(record.get("grantor"))
        if granted_role not in role_names and member not in role_names:
            raise ManifestGenerationError("Membership is unrelated to the managed role set")
        edge = (granted_role, member)
        if edge in edges:
            raise ManifestGenerationError(f"Duplicate membership edge: {edge}")
        edges.add(edge)
        normalized.append(
            {
                "granted_role": granted_role,
                "member": member,
                "grantor": grantor,
                "admin_option": _required_bool(record, "admin_option", str(edge)),
                "inherit_option": _required_bool(record, "inherit_option", str(edge)),
                "set_option": _required_bool(record, "set_option", str(edge)),
            }
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["granted_role"],
            item["member"],
            item["grantor"],
            item["admin_option"],
            item["inherit_option"],
            item["set_option"],
        ),
    )


def _normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    if set(metadata) != {"captured_at", "execution_mode", "source_database", "label"}:
        raise ManifestGenerationError("Capture metadata fields differ from the v2 contract")
    execution_mode = metadata.get("execution_mode")
    if execution_mode not in {"offline_fixture", "peer_admin_read_only"}:
        raise ManifestGenerationError("Invalid capture execution mode")
    source_database = _required_string(metadata.get("source_database"), "source_database")
    if (
        not _SEMANTIC_DATABASE.fullmatch(source_database)
        or source_database == "*"
        or source_database.startswith("gymflow_platform_billing_test_")
    ):
        raise ManifestGenerationError("Source database must be a semantic name")
    label = _required_string(metadata.get("label"), "label")
    if len(label) > 160:
        raise ManifestGenerationError("Capture label is too long")
    return {
        "captured_at": _normalize_timestamp(metadata.get("captured_at"), "captured_at"),
        "execution_mode": execution_mode,
        "source_database": source_database,
        "label": label,
    }


def _semantic_database_scope(record: Mapping[str, Any], mapping: Mapping[str, str]) -> str:
    if "database_scope" in record:
        scope = _required_string(record["database_scope"], "database_scope")
    else:
        actual = record.get("database_name")
        if actual in {None, "", "*"}:
            scope = "*"
        else:
            if not isinstance(actual, str) or actual not in mapping:
                raise ManifestGenerationError("Database-specific setting lacks a semantic mapping")
            scope = mapping[actual]
    if not _SEMANTIC_DATABASE.fullmatch(scope):
        raise ManifestGenerationError("Invalid semantic database scope")
    if scope != "*" and (scope.isdigit() or scope.startswith("gymflow_platform_billing_test_")):
        raise ManifestGenerationError("Database scope must be semantic, not an OID or temporary name")
    return scope


def _normalize_valid_until(value: Any, role_name: str) -> dict[str, str]:
    if value is None or (isinstance(value, str) and value in {"infinity", "unlimited"}):
        return {"kind": "unlimited"}
    if isinstance(value, Mapping):
        kind = value.get("kind")
        if kind == "unlimited" and set(value) == {"kind"}:
            return {"kind": "unlimited"}
        if kind == "timestamp" and set(value) == {"kind", "value"}:
            return {"kind": "timestamp", "value": _normalize_timestamp(value["value"], role_name)}
    if isinstance(value, str):
        return {"kind": "timestamp", "value": _normalize_timestamp(value, role_name)}
    raise ManifestGenerationError(f"Invalid VALID UNTIL for {role_name}")


def _normalize_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestGenerationError(f"{field} must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ManifestGenerationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestGenerationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _absent_role(role_name: str) -> dict[str, Any]:
    return {
        "role_name": role_name,
        "exists": False,
        "superuser": None,
        "inherit": None,
        "create_role": None,
        "create_database": None,
        "login": None,
        "replication": None,
        "bypass_rls": None,
        "connection_limit": None,
        "valid_until": None,
        "password_classification": None,
        "comment": None,
    }


def _required_bool(record: Mapping[str, Any], field: str, identity: str) -> bool:
    value = record.get(field)
    if not isinstance(value, bool):
        raise ManifestGenerationError(f"{field} must be boolean for {identity}")
    return value


def _connection_limit(value: Any, role_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < -1:
        raise ManifestGenerationError(f"Invalid connection limit for {role_name}")
    return value


def _nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestGenerationError(f"{field} must be a string or null")
    return value


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestGenerationError(f"{field} must be a non-empty string")
    return value.strip()


def _validated_role_name(value: Any) -> str:
    name = _required_string(value, "role_name")
    if not _ROLE_NAME.fullmatch(name):
        raise ManifestGenerationError("Invalid role name")
    return name


def _validated_major_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 16:
        raise ManifestGenerationError("PostgreSQL major version must be at least 16")
    return value


def _reject_oid_fields(record: Mapping[str, Any]) -> None:
    for key in record:
        if "oid" in str(key).lower():
            raise ManifestGenerationError(f"OID-bearing field is forbidden: {key}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-json", required=True)
    parser.add_argument("--managed-roles", required=True)
    parser.add_argument("--metadata-json", required=True)
    parser.add_argument("--database-scope-map")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    mapping = load_json(args.database_scope_map) if args.database_scope_map else {}
    if not isinstance(mapping, Mapping):
        raise ManifestGenerationError("Database scope map must be an object")
    manifest = build_manifest(
        load_json(args.catalog_json),
        load_json(args.managed_roles),
        load_json(args.metadata_json),
        database_scope_map=mapping,
    )
    write_json(args.output, manifest)
    print(f"state_sha256={manifest['state_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
