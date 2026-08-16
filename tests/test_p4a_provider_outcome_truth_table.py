from __future__ import annotations

from app.core.external_effect_contract import ProviderOutcome, outcome_may_claim_terminal_success


def test_provider_outcome_truth_table_is_fail_closed() -> None:
    expected = {
        ProviderOutcome.DEFINITE_SUCCESS: True,
        ProviderOutcome.PROVIDER_ACCEPTED_NONTERMINAL: False,
        ProviderOutcome.PERMANENT_REJECTION: False,
        ProviderOutcome.RETRYABLE_FAILURE: False,
        ProviderOutcome.AMBIGUOUS_OUTCOME: False,
    }
    assert {
        outcome: outcome_may_claim_terminal_success(outcome)
        for outcome in ProviderOutcome
    } == expected
