from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from app.core.cluster_role_contract import (
    ContractBundle,
    load_contract_bundle,
    validate_catalog_snapshot,
)


def valid_snapshot() -> dict[str, object]:
    bundle = load_contract_bundle()

    roles = [
        {
            "role": role,
            **record["attributes"],
        }
        for role, record in sorted(
            bundle.roles["managed_roles"].items()
        )
    ]

    memberships = [
        {
            "granted_role": row["granted_role"],
            "member_role": row["member_role"],
            "grantor": row["approved_grantor"],
            "set_option": row["set_option"],
            "inherit_option":
                row["inherit_option"],
            "admin_option": row["admin_option"],
        }
        for row in bundle.memberships["exact_rows"]
    ]

    objects = [
        {
            "object": row["object"],
            "owner": row["target_owner"],
        }
        for row in bundle.ownership["objects"]
    ]

    return {
        "roles": roles,
        "role_settings": deepcopy(
            bundle.role_settings[
                "settings_by_role"
            ]
        ),
        "memberships": memberships,
        "objects": objects,
    }


def codes(
    snapshot: dict[str, object],
    *,
    bundle: ContractBundle | None = None,
) -> set[str]:
    return {
        violation.code
        for violation in validate_catalog_snapshot(
            snapshot,
            bundle,
        )
    }


def test_valid_catalog_snapshot_passes() -> None:
    assert validate_catalog_snapshot(
        valid_snapshot()
    ) == ()


def test_missing_role_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["roles"] = [
        row
        for row in snapshot["roles"]
        if row["role"] != "app_security_owner"
    ]

    assert "role.cardinality" in codes(snapshot)


def test_duplicate_role_row_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["roles"].append(
        deepcopy(snapshot["roles"][0])
    )

    assert "role.cardinality" in codes(snapshot)


def test_migration_owner_createrole_is_rejected() -> None:
    snapshot = valid_snapshot()

    for row in snapshot["roles"]:
        if row["role"] == "migration_owner":
            row["create_role"] = True

    assert "role.attribute" in codes(snapshot)


def test_ops_support_bypassrls_is_rejected() -> None:
    snapshot = valid_snapshot()

    for row in snapshot["roles"]:
        if row["role"] == "ops_support":
            row["bypass_rls"] = True

    assert "role.attribute" in codes(snapshot)


def test_wrong_or_unexpected_role_setting_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["role_settings"]["app_runtime"][
        "statement_timeout"
    ] = "30s"

    assert "role.settings" in codes(snapshot)

    snapshot = valid_snapshot()
    snapshot["role_settings"]["app_runtime"][
        "search_path"
    ] = "public"

    assert "role.settings" in codes(snapshot)


def test_missing_membership_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["memberships"].pop()

    assert "membership.cardinality" in codes(snapshot)


def test_duplicate_safe_membership_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["memberships"].append(
        deepcopy(snapshot["memberships"][0])
    )

    assert "membership.cardinality" in codes(snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grantor", "migration_owner"),
        ("set_option", False),
        ("inherit_option", True),
        ("admin_option", True),
    ],
)
def test_membership_contract_mismatch_is_rejected(
    field: str,
    value: object,
) -> None:
    snapshot = valid_snapshot()
    snapshot["memberships"][0][field] = value

    assert "membership.option" in codes(snapshot)


def test_safe_row_plus_unsafe_parallel_row_is_rejected() -> None:
    snapshot = valid_snapshot()
    unsafe = deepcopy(snapshot["memberships"][0])
    unsafe.update(
        {
            "grantor": "postgres",
            "set_option": False,
            "inherit_option": False,
            "admin_option": True,
        }
    )
    snapshot["memberships"].append(unsafe)

    assert "membership.cardinality" in codes(snapshot)


def test_forbidden_migration_owner_membership_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["memberships"].append(
        {
            "granted_role": "app_runtime",
            "member_role": "migration_owner",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        }
    )

    assert "membership.forbidden" in codes(snapshot)


def test_wrong_manifest_object_owner_is_rejected() -> None:
    snapshot = valid_snapshot()
    expected_owner = snapshot["objects"][0]["owner"]
    wrong_owner = next(
        owner
        for owner in (
            "migration_owner",
            "app_security_owner",
            "app_rls_executor",
        )
        if owner != expected_owner
    )

    snapshot["objects"][0]["owner"] = wrong_owner

    assert wrong_owner != expected_owner
    assert "ownership.owner" in codes(snapshot)


@pytest.mark.parametrize(
    "object_name",
    [
        "IF",
        "NOT",
        "EXISTS",
    ],
)
def test_control_token_object_is_rejected(
    object_name: str,
) -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(
        {
            "object": object_name,
            "owner": "migration_owner",
        }
    )

    assert "ownership.identifier" in codes(snapshot)


@pytest.mark.parametrize(
    "object_name",
    [
        "%I",
        "public.%I",
        "%s",
        "{table_name}",
        "public.",
        "public.table.extra",
        "public.table%I",
    ],
)
def test_non_concrete_object_identifier_is_rejected(
    object_name: str,
) -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(
        {
            "object": object_name,
            "owner": "migration_owner",
        }
    )

    assert "ownership.identifier" in codes(snapshot)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"object": "normal_table"},
        {"owner": "migration_owner"},
        {
            "object": 123,
            "owner": "migration_owner",
        },
        {
            "object": "normal_table",
            "owner": 123,
        },
    ],
)
def test_malformed_ownership_row_is_rejected(
    row: dict[str, object],
) -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(row)

    assert "ownership.record" in codes(snapshot)


def test_empty_ownership_identifier_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(
        {
            "object": "",
            "owner": "migration_owner",
        }
    )

    assert "ownership.identifier" in codes(snapshot)


def test_duplicate_snapshot_ownership_identity_is_rejected() -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(
        deepcopy(snapshot["objects"][0])
    )

    assert (
        "ownership.duplicate_identity"
        in codes(snapshot)
    )


def test_duplicate_contract_ownership_identity_is_rejected() -> None:
    bundle = load_contract_bundle()
    ownership = deepcopy(bundle.ownership)
    duplicate = deepcopy(ownership["objects"][0])
    duplicate["object"] = duplicate[
        "object"
    ].upper()
    ownership["objects"].append(duplicate)
    duplicate_bundle = replace(
        bundle,
        ownership=ownership,
    )

    assert (
        "ownership.duplicate_identity"
        in codes(
            valid_snapshot(),
            bundle=duplicate_bundle,
        )
    )


def test_valid_unqualified_and_qualified_identifiers_pass() -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].extend(
        [
            {
                "object": "normal_table",
                "owner": "migration_owner",
            },
            {
                "object": "public.normal_table",
                "owner": "migration_owner",
            },
            {
                "object": "public",
                "owner": "migration_owner",
            },
        ]
    )

    assert validate_catalog_snapshot(snapshot) == ()


@pytest.mark.parametrize(
    "object_name",
    [
        "IF",
        "NOT",
        "EXISTS",
    ],
)
def test_contract_control_token_object_is_rejected(
    object_name: str,
) -> None:
    assert object_name in {
        "IF",
        "NOT",
        "EXISTS",
    }

    bundle = load_contract_bundle()
    ownership = deepcopy(bundle.ownership)
    ownership["objects"][0][
        "object"
    ] = object_name
    modified_bundle = replace(
        bundle,
        ownership=ownership,
    )

    assert codes(
        valid_snapshot(),
        bundle=modified_bundle,
    ) == {"ownership.identifier"}


@pytest.mark.parametrize(
    "object_name",
    [
        "%I",
        "public.%I",
        "%s",
        "{table_name}",
    ],
)
def test_contract_placeholder_object_is_rejected(
    object_name: str,
) -> None:
    assert object_name in {
        "%I",
        "public.%I",
        "%s",
        "{table_name}",
    }

    bundle = load_contract_bundle()
    ownership = deepcopy(bundle.ownership)
    ownership["objects"][0][
        "object"
    ] = object_name
    modified_bundle = replace(
        bundle,
        ownership=ownership,
    )

    assert codes(
        valid_snapshot(),
        bundle=modified_bundle,
    ) == {"ownership.identifier"}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_object",
        "non_string_object",
        "empty_policy",
    ],
)
def test_malformed_contract_ownership_record_is_rejected(
    mutation: str,
) -> None:
    bundle = load_contract_bundle()
    ownership = deepcopy(bundle.ownership)
    record = ownership["objects"][0]

    if mutation == "missing_object":
        record.pop("object")
    elif mutation == "non_string_object":
        record.__setitem__("object", 123)
    elif mutation == "empty_policy":
        record.__setitem__("policy", "")
    else:
        raise AssertionError(
            f"Unexpected mutation: {mutation}"
        )

    modified_bundle = replace(
        bundle,
        ownership=ownership,
    )

    assert codes(
        valid_snapshot(),
        bundle=modified_bundle,
    ) == {"ownership.contract_record"}


def test_forbidden_role_may_not_own_extra_object() -> None:
    snapshot = valid_snapshot()
    snapshot["objects"].append(
        {
            "object": "public.unexpected_runtime_table",
            "owner": "app_runtime",
        }
    )

    assert "ownership.forbidden_owner" in codes(
        snapshot
    )
