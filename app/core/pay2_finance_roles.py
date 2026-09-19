"""PAY-2 additive Finance capability-role contract helpers.

This module is pure. It opens no database connection and mutates no cluster
state. Production expansion remains an explicit infrastructure-admin action
outside Alembic.
"""
from __future__ import annotations

from copy import deepcopy

from app.core.cluster_identity_graph import IdentityTransitionPolicy, load_identity_transition_policy
from app.core.cluster_role_contract import ContractBundle, load_contract_bundle


PAY2_FINANCE_CAPABILITY_ROLES = (
    "finance_runtime",
    "finance_payment_runtime",
    "finance_refund_runtime",
    "finance_reconciliation_runtime",
    "finance_read_runtime",
    "finance_maintenance_runtime",
)


def predecessor_contract_bundle(bundle: ContractBundle | None = None) -> ContractBundle:
    source = bundle or load_contract_bundle()
    roles = deepcopy(source.roles)
    settings = deepcopy(source.role_settings)
    managed = roles["managed_roles"]
    for role in PAY2_FINANCE_CAPABILITY_ROLES:
        managed.pop(role, None)
        settings["settings_by_role"].pop(role, None)
    roles.pop("pay2_extension", None)
    settings.pop("pay2_extension", None)
    memberships = deepcopy(source.memberships)
    memberships["forbidden_migration_owner_memberships"] = [
        role for role in memberships["forbidden_migration_owner_memberships"]
        if role not in PAY2_FINANCE_CAPABILITY_ROLES
    ]
    memberships.pop("pay2_extension", None)
    return ContractBundle(
        roles=roles,
        role_settings=settings,
        memberships=memberships,
        grantors=deepcopy(source.grantors),
        ownership=deepcopy(source.ownership),
    )


def expansion_contract_bundle(bundle: ContractBundle | None = None) -> ContractBundle:
    source = bundle or load_contract_bundle()
    roles = deepcopy(source.roles)
    roles["managed_roles"] = {
        role: deepcopy(source.roles["managed_roles"][role])
        for role in PAY2_FINANCE_CAPABILITY_ROLES
    }
    roles["retired_roles"] = {}
    settings = deepcopy(source.role_settings)
    settings["settings_by_role"] = {
        role: deepcopy(source.role_settings["settings_by_role"][role])
        for role in PAY2_FINANCE_CAPABILITY_ROLES
    }
    memberships = deepcopy(source.memberships)
    memberships["exact_rows"] = []
    memberships["forbidden_migration_owner_memberships"] = []
    return ContractBundle(
        roles=roles,
        role_settings=settings,
        memberships=memberships,
        grantors=deepcopy(source.grantors),
        ownership=deepcopy(source.ownership),
    )


def predecessor_identity_policy(
    policy: IdentityTransitionPolicy | None = None,
) -> IdentityTransitionPolicy:
    source = policy or load_identity_transition_policy()
    blocked = set(PAY2_FINANCE_CAPABILITY_ROLES)
    return IdentityTransitionPolicy(
        migration_principal=source.migration_principal,
        peer_isolation_principals=tuple(
            role for role in source.peer_isolation_principals if role not in blocked
        ),
        migration_helpers=source.migration_helpers,
        ordinary_capabilities=source.ordinary_capabilities,
        rules=dict(source.rules),
    )
