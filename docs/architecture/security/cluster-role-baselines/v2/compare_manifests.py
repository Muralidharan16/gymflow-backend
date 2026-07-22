#!/usr/bin/env python3
"""Safe structural comparison for secret-free cluster-role manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import generator
import validate_manifest


def compare_manifests(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    managed_roles: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    generator.ensure_secret_free(left)
    generator.ensure_secret_free(right)
    left_state = _state(left, "left")
    right_state = _state(right, "right")

    non_comparable: list[str] = []
    if left_state.get("manifest_schema_version") != right_state.get("manifest_schema_version"):
        non_comparable.append("manifest_schema_version_mismatch")
    if left_state.get("generator_version") != right_state.get("generator_version"):
        non_comparable.append("generator_version_mismatch")
    if non_comparable:
        return {
            "comparable": False,
            "equal": False,
            "reason_codes": non_comparable,
            "differences": [],
        }

    try:
        validate_manifest.validate_manifest(left, managed_roles, schema)
        validate_manifest.validate_manifest(right, managed_roles, schema)
    except (validate_manifest.ManifestValidationError, generator.ManifestGenerationError) as exc:
        return {
            "comparable": False,
            "equal": False,
            "reason_codes": ["manifest_validation_failed"],
            "validation_error": str(exc),
            "differences": [],
        }

    differences: list[dict[str, Any]] = []
    for field in (
        "policy_version",
        "policy_status",
        "evidence_status",
        "postgresql_major_version",
        "relevant_role_set_id",
        "relevant_role_set_sha256",
        "relevant_roles",
    ):
        _append_difference(differences, "state", "manifest", field, left_state.get(field), right_state.get(field))

    _compare_keyed(
        differences,
        "role",
        left_state["roles"],
        right_state["roles"],
        lambda item: item["role_name"],
    )
    _compare_keyed(
        differences,
        "role_setting",
        left_state["role_settings"],
        right_state["role_settings"],
        lambda item: (item["role_name"], item["database_scope"], item["setting_name"]),
    )
    _compare_keyed(
        differences,
        "membership",
        left_state["memberships"],
        right_state["memberships"],
        lambda item: (item["granted_role"], item["member"]),
    )
    return {
        "comparable": True,
        "equal": not differences,
        "left_state_sha256": left["state_sha256"],
        "right_state_sha256": right["state_sha256"],
        "reason_codes": [] if not differences else ["structured_state_difference"],
        "differences": differences,
    }


def _compare_keyed(
    differences: list[dict[str, Any]],
    category: str,
    left_items: Sequence[Mapping[str, Any]],
    right_items: Sequence[Mapping[str, Any]],
    identity_fn,
) -> None:
    left = {identity_fn(item): item for item in left_items}
    right = {identity_fn(item): item for item in right_items}
    for identity in sorted(set(left) | set(right), key=str):
        left_item = left.get(identity)
        right_item = right.get(identity)
        if left_item is None or right_item is None:
            _append_difference(
                differences,
                category,
                identity,
                "existence",
                left_item is not None,
                right_item is not None,
            )
            continue
        for field in sorted(set(left_item) | set(right_item)):
            _append_difference(differences, category, identity, field, left_item.get(field), right_item.get(field))


def _append_difference(
    differences: list[dict[str, Any]],
    category: str,
    identity: Any,
    field: str,
    left: Any,
    right: Any,
) -> None:
    if left == right:
        return
    differences.append(
        {
            "category": category,
            "identity": _safe_identity(identity),
            "field": field,
            "left": left,
            "right": right,
        }
    )


def _safe_identity(value: Any) -> str:
    if isinstance(value, tuple):
        return "|".join(str(part) for part in value)
    return str(value)


def _state(manifest: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    state = manifest.get("state")
    if not isinstance(state, Mapping):
        raise generator.ManifestGenerationError(f"{label} manifest state must be an object")
    return state


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--managed-roles", default=str(base / "managed_roles.json"))
    parser.add_argument("--schema", default=str(base / "manifest_schema_v2.json"))
    parser.add_argument("--format", choices=("human", "json"), default="human")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = compare_manifests(
            generator.load_json(args.left),
            generator.load_json(args.right),
            generator.load_json(args.managed_roles),
            generator.load_json(args.schema),
        )
    except (OSError, json.JSONDecodeError, generator.ManifestGenerationError) as exc:
        result = {
            "comparable": False,
            "equal": False,
            "reason_codes": ["input_rejected"],
            "error": str(exc),
            "differences": [],
        }
    if args.format == "json":
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    else:
        print(f"comparable={'yes' if result['comparable'] else 'no'}")
        print(f"equal={'yes' if result['equal'] else 'no'}")
        for reason in result.get("reason_codes", []):
            print(f"reason={reason}")
        for difference in result.get("differences", []):
            print(
                "difference="
                f"{difference['category']}:{difference['identity']}:{difference['field']} "
                f"left={difference['left']!r} right={difference['right']!r}"
            )
    if not result["comparable"]:
        return 2
    return 0 if result["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
