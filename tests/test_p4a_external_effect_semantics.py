from __future__ import annotations

import json
from pathlib import Path

from app.core.external_effect_contract import (
    ExternalEffectState,
    ProviderOutcome,
    outcome_may_claim_terminal_success,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"


def test_code_semantics_match_versioned_p4_inventory() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    required = inventory["required_semantics"]

    assert {state.value for state in ExternalEffectState} == set(
        required["command_states"]
    )
    assert {outcome.value for outcome in ProviderOutcome} == set(
        required["provider_outcomes"]
    )


def test_only_definite_provider_success_may_claim_terminal_success() -> None:
    assert outcome_may_claim_terminal_success(ProviderOutcome.DEFINITE_SUCCESS)

    for outcome in ProviderOutcome:
        if outcome is ProviderOutcome.DEFINITE_SUCCESS:
            continue
        assert not outcome_may_claim_terminal_success(outcome)


def test_provider_acceptance_is_explicitly_nonterminal() -> None:
    assert not outcome_may_claim_terminal_success(
        ProviderOutcome.PROVIDER_ACCEPTED_NONTERMINAL
    )
