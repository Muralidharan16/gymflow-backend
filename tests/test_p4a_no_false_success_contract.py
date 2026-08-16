from __future__ import annotations

from app.core.external_effect_contract import ExternalEffectState, ProviderOutcome


def test_terminal_success_is_not_an_attempt_state() -> None:
    assert ExternalEffectState.SUCCEEDED not in {
        ExternalEffectState.PENDING,
        ExternalEffectState.PROCESSING,
        ExternalEffectState.PROVIDER_ACCEPTED,
        ExternalEffectState.RETRY_PENDING,
    }


def test_ambiguous_or_retryable_provider_outcomes_are_not_success() -> None:
    assert ProviderOutcome.AMBIGUOUS_OUTCOME is not ProviderOutcome.DEFINITE_SUCCESS
    assert ProviderOutcome.RETRYABLE_FAILURE is not ProviderOutcome.DEFINITE_SUCCESS
    assert ProviderOutcome.PROVIDER_ACCEPTED_NONTERMINAL is not ProviderOutcome.DEFINITE_SUCCESS
