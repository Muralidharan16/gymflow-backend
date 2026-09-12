from __future__ import annotations

from enum import StrEnum


class ExternalEffectState(StrEnum):
    """Canonical semantic states for durable P4 external effects.

    Domains may use different storage labels where existing models require it,
    but they must preserve these meanings and may not collapse request attempt
    into terminal downstream success.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    PROVIDER_ACCEPTED = "provider_accepted"
    SUCCEEDED = "succeeded"
    RETRY_PENDING = "retry_pending"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ProviderOutcome(StrEnum):
    """Normalized adapter result classification shared by P4 integrations."""

    DEFINITE_SUCCESS = "definite_success"
    PROVIDER_ACCEPTED_NONTERMINAL = "provider_accepted_nonterminal"
    PERMANENT_REJECTION = "permanent_rejection"
    RETRYABLE_FAILURE = "retryable_failure"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"


TERMINAL_SUCCESS_STATES = frozenset({ExternalEffectState.SUCCEEDED})
NONTERMINAL_PROVIDER_STATES = frozenset({ExternalEffectState.PROVIDER_ACCEPTED})


def outcome_may_claim_terminal_success(outcome: ProviderOutcome) -> bool:
    """Return whether an adapter outcome itself can justify terminal success.

    `PROVIDER_ACCEPTED_NONTERMINAL` is intentionally false: an HTTP/provider
    acknowledgement is not business completion unless the concrete provider
    adapter classifies its evidence as `DEFINITE_SUCCESS` under a certified
    domain contract.
    """

    return outcome is ProviderOutcome.DEFINITE_SUCCESS
