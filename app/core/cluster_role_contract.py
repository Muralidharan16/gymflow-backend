"""Pure read-only validation for the PostgreSQL cluster-role contract.

This module accepts already captured catalog snapshots. It opens no
database connection and performs no catalog mutation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


CONTRACT_DIRECTORY = (
    Path(__file__).resolve().parents[2]
    / "security"
    / "cluster_role_bootstrap"
)

MANIFEST_FILES = {
    "roles": "roles.v1.json",
    "role_settings": "role_settings.v1.json",
    "memberships": "memberships.v1.json",
    "grantors": "grantors.v1.json",
    "ownership": "ownership.v1.json",
}

ROLE_ATTRIBUTE_NAMES = (
    "superuser",
    "inherit",
    "create_role",
    "create_db",
    "can_login",
    "replication",
    "bypass_rls",
)


OWNERSHIP_CONTROL_TOKENS = frozenset(
    {
        "IF",
        "NOT",
        "EXISTS",
        "THEN",
        "BEGIN",
        "END",
        "ELSE",
        "NULL",
        "ONLY",
        "CREATE",
        "ALTER",
        "DROP",
        "TABLE",
        "SCHEMA",
        "SEQUENCE",
        "VIEW",
        "FUNCTION",
        "PROCEDURE",
        "INDEX",
    }
)

OWNERSHIP_OBJECT_TYPES = frozenset(
    {
        "SCHEMA",
        "TABLE",
        "VIEW",
        "INDEX",
        "SEQUENCE",
        "FUNCTION",
        "TABLE PARTITION FAMILY",
    }
)

_IDENTIFIER = r"[_A-Za-z][_A-Za-z0-9$]*"
_QUALIFIED_IDENTIFIER = (
    rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})?"
)
_FUNCTION_ARGUMENT = (
    rf"{_QUALIFIED_IDENTIFIER}"
    rf"(?:\s+{_IDENTIFIER})*"
    r"(?:\[\])?"
)

_RELATION_IDENTIFIER_PATTERN = re.compile(
    _QUALIFIED_IDENTIFIER
)
_FUNCTION_IDENTIFIER_PATTERN = re.compile(
    rf"{_QUALIFIED_IDENTIFIER}"
    rf"(?:\(\s*(?:{_FUNCTION_ARGUMENT}"
    rf"(?:\s*,\s*{_FUNCTION_ARGUMENT})*)?"
    r"\s*\))?"
)
_PLACEHOLDER_PATTERN = re.compile(
    r"%(?:I|s)|[{}]",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class ContractViolation:
    code: str
    subject: str
    message: str


@dataclass(frozen=True)
class ContractBundle:
    roles: Mapping[str, Any]
    role_settings: Mapping[str, Any]
    memberships: Mapping[str, Any]
    grantors: Mapping[str, Any]
    ownership: Mapping[str, Any]


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(value, dict):
        raise ValueError(
            f"Manifest must contain an object: {path}"
        )

    return value


def load_contract_bundle(
    directory: Path | None = None,
) -> ContractBundle:
    root = directory or CONTRACT_DIRECTORY

    return ContractBundle(
        roles=_load_json(root / MANIFEST_FILES["roles"]),
        role_settings=_load_json(
            root / MANIFEST_FILES["role_settings"]
        ),
        memberships=_load_json(
            root / MANIFEST_FILES["memberships"]
        ),
        grantors=_load_json(
            root / MANIFEST_FILES["grantors"]
        ),
        ownership=_load_json(
            root / MANIFEST_FILES["ownership"]
        ),
    )


def _append(
    violations: list[ContractViolation],
    code: str,
    subject: str,
    message: str,
) -> None:
    violations.append(
        ContractViolation(
            code=code,
            subject=subject,
            message=message,
        )
    )


def _group_by(
    records: Sequence[Mapping[str, Any]],
    key: str,
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}

    for record in records:
        value = record.get(key)

        if not isinstance(value, str):
            continue

        grouped.setdefault(value, []).append(record)

    return grouped


def _membership_identity(
    record: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    granted_role = record.get("granted_role")
    member_role = record.get("member_role")

    return (
        granted_role
        if isinstance(granted_role, str)
        else None,
        member_role
        if isinstance(member_role, str)
        else None,
    )



def _identifier_leaf(value: str) -> str:
    return (
        value.split("(", 1)[0]
        .rsplit(".", 1)[-1]
        .upper()
    )


def _is_concrete_identifier(
    value: object,
    *,
    object_type: str | None = None,
    allow_function_signature: bool = False,
) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _PLACEHOLDER_PATTERN.search(value)
    ):
        return False

    pattern = (
        _FUNCTION_IDENTIFIER_PATTERN
        if allow_function_signature
        or object_type == "FUNCTION"
        else _RELATION_IDENTIFIER_PATTERN
    )

    return (
        pattern.fullmatch(value) is not None
        and _identifier_leaf(value)
        not in OWNERSHIP_CONTROL_TOKENS
    )


def _canonical_identifier(
    value: str,
    object_type: str | None = None,
) -> str:
    if (
        object_type == "FUNCTION"
        and "(" in value
        and value.endswith(")")
    ):
        base, arguments = value[:-1].split("(", 1)
        canonical_arguments = ",".join(
            " ".join(argument.split()).upper()
            for argument in arguments.split(",")
            if argument.strip()
        )
        return (
            f"{base.casefold()}"
            f"({canonical_arguments})"
        )

    return value.casefold()


def _validate_contract_ownership(
    ownership: Mapping[str, Any],
    violations: list[ContractViolation],
) -> list[Mapping[str, Any]]:
    records = ownership.get("objects")
    allowed_owners = ownership.get(
        "allowed_target_owners"
    )

    if not isinstance(records, list):
        _append(
            violations,
            "ownership.contract_record",
            "contract.objects",
            "Ownership contract objects must be a list.",
        )
        return []

    if not isinstance(allowed_owners, list):
        _append(
            violations,
            "ownership.contract_record",
            "contract.allowed_target_owners",
            "Allowed target owners must be a list.",
        )
        return []

    allowed_owner_set = {
        owner
        for owner in allowed_owners
        if isinstance(owner, str)
    }
    required = {
        "object",
        "object_type",
        "parent_relation",
        "target_owner",
        "dynamic",
        "policy",
    }
    valid: list[Mapping[str, Any]] = []
    seen: dict[
        tuple[str, str, str | None],
        int,
    ] = {}

    for index, record in enumerate(records):
        subject = f"contract.objects[{index}]"

        if not isinstance(record, Mapping):
            _append(
                violations,
                "ownership.contract_record",
                subject,
                "Ownership contract record must be an object.",
            )
            continue

        missing = sorted(required - set(record))

        if missing:
            _append(
                violations,
                "ownership.contract_record",
                subject,
                "Missing fields: " + ", ".join(missing) + ".",
            )
            continue

        object_name = record.get("object")
        object_type = record.get("object_type")
        parent = record.get("parent_relation")
        owner = record.get("target_owner")
        dynamic = record.get("dynamic")
        policy = record.get("policy")

        if (
            not isinstance(object_name, str)
            or not isinstance(object_type, str)
            or (
                parent is not None
                and not isinstance(parent, str)
            )
            or not isinstance(owner, str)
            or not isinstance(dynamic, bool)
            or not isinstance(policy, str)
            or not policy
        ):
            _append(
                violations,
                "ownership.contract_record",
                subject,
                "Ownership contract fields have invalid values.",
            )
            continue

        normalized_type = object_type.upper()

        if (
            normalized_type not in OWNERSHIP_OBJECT_TYPES
            or owner not in allowed_owner_set
        ):
            _append(
                violations,
                "ownership.contract_record",
                subject,
                "Ownership type or target owner is not allowed.",
            )
            continue

        if not _is_concrete_identifier(
            object_name,
            object_type=normalized_type,
        ):
            _append(
                violations,
                "ownership.identifier",
                object_name or subject,
                "Object must be one complete concrete identifier.",
            )
            continue

        requires_parent = normalized_type in {
            "INDEX",
            "TABLE PARTITION FAMILY",
        }

        if (
            requires_parent != isinstance(parent, str)
            or (
                isinstance(parent, str)
                and not _is_concrete_identifier(parent)
            )
        ):
            _append(
                violations,
                (
                    "ownership.identifier"
                    if isinstance(parent, str)
                    else "ownership.contract_record"
                ),
                parent or subject,
                "Parent relation is missing, unexpected, or invalid.",
            )
            continue

        identity = (
            normalized_type,
            _canonical_identifier(
                object_name,
                normalized_type,
            ),
            (
                parent.casefold()
                if isinstance(parent, str)
                else None
            ),
        )

        if identity in seen:
            _append(
                violations,
                "ownership.duplicate_identity",
                object_name,
                (
                    "Duplicate canonical ownership identity; "
                    f"first declared at index {seen[identity]}."
                ),
            )
            continue

        seen[identity] = index
        valid.append(record)

    return valid


def _validate_snapshot_objects(
    records: Sequence[object],
    violations: list[ContractViolation],
) -> list[Mapping[str, Any]]:
    valid: list[Mapping[str, Any]] = []
    seen: dict[str, int] = {}

    for index, record in enumerate(records):
        subject = f"snapshot.objects[{index}]"

        if (
            not isinstance(record, Mapping)
            or "object" not in record
            or "owner" not in record
            or not isinstance(record.get("owner"), str)
            or not record.get("owner")
        ):
            _append(
                violations,
                "ownership.record",
                subject,
                "Ownership row requires string object and owner fields.",
            )
            continue

        object_name = record.get("object")

        if not isinstance(object_name, str):
            _append(
                violations,
                "ownership.record",
                subject,
                "Ownership snapshot object must be a string.",
            )
            continue

        if not _is_concrete_identifier(
            object_name,
            allow_function_signature=True,
        ):
            _append(
                violations,
                "ownership.identifier",
                object_name,
                "Object must be one complete concrete identifier.",
            )
            continue

        identity = _canonical_identifier(
            object_name,
            (
                "FUNCTION"
                if "(" in object_name
                else None
            ),
        )

        if identity in seen:
            _append(
                violations,
                "ownership.duplicate_identity",
                object_name,
                (
                    "Duplicate canonical ownership identity; "
                    f"first observed at index {seen[identity]}."
                ),
            )
            continue

        seen[identity] = index
        valid.append(record)

    return valid


def validate_catalog_snapshot(
    snapshot: Mapping[str, Any],
    bundle: ContractBundle | None = None,
    *,
    require_complete_ownership: bool = True,
) -> tuple[ContractViolation, ...]:
    contract = bundle or load_contract_bundle()
    violations: list[ContractViolation] = []

    role_rows = snapshot.get("roles", [])
    setting_rows = snapshot.get("role_settings", {})
    membership_rows = snapshot.get("memberships", [])
    object_rows = snapshot.get("objects", [])

    if not isinstance(role_rows, list):
        raise TypeError("snapshot.roles must be a list")

    if not isinstance(setting_rows, dict):
        raise TypeError(
            "snapshot.role_settings must be an object"
        )

    if not isinstance(membership_rows, list):
        raise TypeError(
            "snapshot.memberships must be a list"
        )

    if not isinstance(object_rows, list):
        raise TypeError("snapshot.objects must be a list")

    expected_objects = _validate_contract_ownership(
        contract.ownership,
        violations,
    )
    valid_object_rows = _validate_snapshot_objects(
        object_rows,
        violations,
    )

    roles_by_name = _group_by(role_rows, "role")
    managed_roles = contract.roles["managed_roles"]

    for role, expected_record in managed_roles.items():
        rows = roles_by_name.get(role, [])

        if len(rows) != 1:
            _append(
                violations,
                "role.cardinality",
                role,
                f"Expected exactly one role row; found {len(rows)}.",
            )
            continue

        actual = rows[0]
        expected_attributes = expected_record["attributes"]

        for attribute in ROLE_ATTRIBUTE_NAMES:
            expected = expected_attributes[attribute]
            observed = actual.get(attribute)

            if observed is not expected:
                _append(
                    violations,
                    "role.attribute",
                    f"{role}.{attribute}",
                    f"Expected {expected!r}; found {observed!r}.",
                )

    expected_settings = contract.role_settings[
        "settings_by_role"
    ]

    for role, expected in expected_settings.items():
        actual = setting_rows.get(role, {})

        if actual != expected:
            _append(
                violations,
                "role.settings",
                role,
                f"Expected {expected!r}; found {actual!r}.",
            )

    expected_memberships = contract.memberships[
        "exact_rows"
    ]

    expected_pairs = {
        (
            row["granted_role"],
            row["member_role"],
        ): row
        for row in expected_memberships
    }

    for pair, expected in expected_pairs.items():
        matching = [
            row
            for row in membership_rows
            if _membership_identity(row) == pair
        ]

        if len(matching) != expected["exact_row_count"]:
            _append(
                violations,
                "membership.cardinality",
                f"{pair[0]}->{pair[1]}",
                (
                    "Expected exactly "
                    f"{expected['exact_row_count']} row; "
                    f"found {len(matching)}."
                ),
            )
            continue

        actual = matching[0]

        exact_fields = {
            "grantor": expected["approved_grantor"],
            "set_option": expected["set_option"],
            "inherit_option":
                expected["inherit_option"],
            "admin_option": expected["admin_option"],
        }

        for field, expected_value in exact_fields.items():
            observed = actual.get(field)

            if observed is not expected_value and (
                not isinstance(expected_value, str)
                or observed != expected_value
            ):
                _append(
                    violations,
                    "membership.option",
                    f"{pair[0]}->{pair[1]}.{field}",
                    (
                        f"Expected {expected_value!r}; "
                        f"found {observed!r}."
                    ),
                )

    allowed_pairs = set(expected_pairs)

    for row in membership_rows:
        pair = _membership_identity(row)

        if pair[1] != "migration_owner":
            continue

        if pair not in allowed_pairs:
            _append(
                violations,
                "membership.forbidden",
                f"{pair[0]}->{pair[1]}",
                "No membership row is allowed for this pair.",
            )

    object_rows_by_name = _group_by(
        valid_object_rows,
        "object",
    )

    if require_complete_ownership:
        for expected in expected_objects:
            object_name = expected["object"]
            rows = object_rows_by_name.get(
                object_name,
                [],
            )

            if len(rows) != 1:
                _append(
                    violations,
                    "ownership.cardinality",
                    object_name,
                    (
                        "Expected exactly one object row; "
                        f"found {len(rows)}."
                    ),
                )
                continue

            observed_owner = rows[0].get("owner")
            expected_owner = expected["target_owner"]

            if observed_owner != expected_owner:
                _append(
                    violations,
                    "ownership.owner",
                    object_name,
                    (
                        f"Expected owner {expected_owner!r}; "
                        f"found {observed_owner!r}."
                    ),
                )

    forbidden_owners = set(
        contract.ownership["forbidden_object_owners"]
    )

    for row in valid_object_rows:
        owner = row.get("owner")

        if owner in forbidden_owners:
            _append(
                violations,
                "ownership.forbidden_owner",
                str(row.get("object")),
                f"Role {owner!r} must own no object.",
            )

    return tuple(sorted(violations))


def _managed_role_projection(
    snapshot: Mapping[str, Any],
    contract: ContractBundle,
) -> list[Mapping[str, Any]]:
    managed = set(contract.roles["managed_roles"])

    return sorted(
        [
            row
            for row in snapshot.get("roles", [])
            if row.get("role") in managed
        ],
        key=lambda row: str(row.get("role")),
    )


def _managed_membership_projection(
    snapshot: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    fields = (
        "granted_role",
        "member_role",
        "grantor",
        "set_option",
        "inherit_option",
        "admin_option",
    )

    rows = []

    for row in snapshot.get("memberships", []):
        if row.get("member_role") != "migration_owner":
            continue

        rows.append(
            {
                field: row.get(field)
                for field in fields
            }
        )

    return sorted(
        rows,
        key=lambda row: (
            str(row["granted_role"]),
            str(row["grantor"]),
            str(row["set_option"]),
            str(row["inherit_option"]),
            str(row["admin_option"]),
        ),
    )


def validate_downgrade_preservation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    bundle: ContractBundle | None = None,
) -> tuple[ContractViolation, ...]:
    contract = bundle or load_contract_bundle()
    violations: list[ContractViolation] = []

    if _managed_role_projection(
        before,
        contract,
    ) != _managed_role_projection(
        after,
        contract,
    ):
        _append(
            violations,
            "downgrade.roles_changed",
            "managed_roles",
            (
                "Cluster-role rows changed during "
                "database-local downgrade."
            ),
        )

    if before.get("role_settings", {}) != after.get(
        "role_settings",
        {},
    ):
        _append(
            violations,
            "downgrade.settings_changed",
            "role_settings",
            (
                "Cluster-role settings changed during "
                "database-local downgrade."
            ),
        )

    if _managed_membership_projection(
        before
    ) != _managed_membership_projection(after):
        _append(
            violations,
            "downgrade.memberships_changed",
            "migration_owner_memberships",
            (
                "Bootstrap-managed memberships changed "
                "during database-local downgrade."
            ),
        )

    after_violations = validate_catalog_snapshot(
        after,
        contract,
        require_complete_ownership=False,
    )

    for violation in after_violations:
        violations.append(
            ContractViolation(
                code=f"downgrade.after.{violation.code}",
                subject=violation.subject,
                message=violation.message,
            )
        )

    return tuple(sorted(violations))
