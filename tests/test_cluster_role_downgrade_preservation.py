from __future__ import annotations

from copy import deepcopy

from app.core.cluster_role_contract import (
    load_contract_bundle,
    validate_downgrade_preservation,
)


def valid_snapshot() -> dict[str, object]:
    bundle = load_contract_bundle()

    return {
        "roles": [
            {
                "role": role,
                **record["attributes"],
            }
            for role, record in sorted(
                bundle.roles[
                    "managed_roles"
                ].items()
            )
        ],
        "role_settings": deepcopy(
            bundle.role_settings[
                "settings_by_role"
            ]
        ),
        "memberships": [
            {
                "granted_role":
                    row["granted_role"],
                "member_role":
                    row["member_role"],
                "grantor":
                    row["approved_grantor"],
                "set_option":
                    row["set_option"],
                "inherit_option":
                    row["inherit_option"],
                "admin_option":
                    row["admin_option"],
            }
            for row in bundle.memberships[
                "exact_rows"
            ]
        ],
        "objects": [],
    }


def codes(
    before: dict[str, object],
    after: dict[str, object],
) -> set[str]:
    return {
        violation.code
        for violation in validate_downgrade_preservation(
            before,
            after,
        )
    }


def test_database_object_removal_does_not_change_cluster_contract() -> None:
    before = valid_snapshot()
    before["objects"] = [
        {
            "object":
                "app_secure.revision_owned_view",
            "owner": "app_security_owner",
        }
    ]

    after = valid_snapshot()
    after["objects"] = []

    assert validate_downgrade_preservation(
        before,
        after,
    ) == ()


def test_role_attribute_change_is_rejected() -> None:
    before = valid_snapshot()
    after = deepcopy(before)

    for row in after["roles"]:
        if row["role"] == "app_security_owner":
            row["can_login"] = True

    result = codes(before, after)

    assert "downgrade.roles_changed" in result
    assert (
        "downgrade.after.role.attribute"
        in result
    )


def test_role_setting_change_is_rejected() -> None:
    before = valid_snapshot()
    after = deepcopy(before)

    after["role_settings"]["app_runtime"][
        "statement_timeout"
    ] = "30s"

    result = codes(before, after)

    assert "downgrade.settings_changed" in result
    assert (
        "downgrade.after.role.settings"
        in result
    )


def test_membership_removal_is_rejected() -> None:
    before = valid_snapshot()
    after = deepcopy(before)
    after["memberships"].pop()

    result = codes(before, after)

    assert "downgrade.memberships_changed" in result
    assert (
        "downgrade.after.membership.cardinality"
        in result
    )


def test_unsafe_parallel_membership_is_rejected() -> None:
    before = valid_snapshot()
    after = deepcopy(before)

    unsafe = deepcopy(after["memberships"][0])
    unsafe.update(
        {
            "set_option": False,
            "admin_option": True,
        }
    )
    after["memberships"].append(unsafe)

    result = codes(before, after)

    assert "downgrade.memberships_changed" in result
    assert (
        "downgrade.after.membership.cardinality"
        in result
    )


def test_forbidden_new_membership_is_rejected() -> None:
    before = valid_snapshot()
    after = deepcopy(before)

    after["memberships"].append(
        {
            "granted_role": "ops_support",
            "member_role": "migration_owner",
            "grantor": "postgres",
            "set_option": True,
            "inherit_option": False,
            "admin_option": False,
        }
    )

    result = codes(before, after)

    assert "downgrade.memberships_changed" in result
    assert (
        "downgrade.after.membership.forbidden"
        in result
    )
