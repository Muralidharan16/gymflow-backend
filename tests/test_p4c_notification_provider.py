from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.notification_provider import (
    NotificationProviderError,
    ResendEmailProvider,
)


DATA = {
    "branch_name": "Central <Gym>",
    "from_status": "active",
    "to_status": "temporarily_closed",
}


def _provider(handler) -> tuple[ResendEmailProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://api.resend.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        ResendEmailProvider(
            mode="resend",
            api_key="re_test_key",
            from_email="members@doers.example",
            base_url="https://api.resend.test",
            timeout_seconds=2,
            client=client,
        ),
        client,
    )


def _send(provider: ResendEmailProvider, **overrides):
    values = {
        "destination": "current@example.test",
        "member_name": "A <Member>",
        "template_key": "branch_lifecycle_status_changed",
        "template_data": DATA,
        "idempotency_key": "branch-lifecycle/correlation/member/email",
    }
    values.update(overrides)
    return asyncio.run(provider.send(**values))


def test_resend_acceptance_returns_nonterminal_evidence_and_exact_idempotency_key() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["idempotency"] = request.headers.get("Idempotency-Key")
        observed["body"] = request.read().decode()
        return httpx.Response(200, request=request, json={"id": "email_123"})

    provider, client = _provider(handler)
    try:
        evidence = _send(provider)
    finally:
        asyncio.run(client.aclose())

    assert evidence.provider_code == "resend"
    assert evidence.provider_reference_id == "email_123"
    assert len(evidence.request_sha256) == 64
    assert len(evidence.provider_evidence_sha256) == 64
    assert observed["idempotency"] == "branch-lifecycle/correlation/member/email"
    assert "current@example.test" in observed["body"]
    assert "&lt;Gym&gt;" in observed["body"]
    assert "&lt;Member&gt;" in observed["body"]


def test_transport_timeout_is_ambiguous_not_false_failure_or_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unknown commit", request=request)

    provider, client = _provider(handler)
    try:
        with pytest.raises(NotificationProviderError) as exc_info:
            _send(provider)
    finally:
        asyncio.run(client.aclose())

    error = exc_info.value
    assert error.outcome == "ambiguous_outcome"
    assert error.error_code == "provider_transport_ambiguous"
    assert len(error.request_sha256) == 64


def test_429_and_5xx_are_retryable_and_retry_after_is_bounded() -> None:
    for status, header, expected in ((429, "90", 90), (503, "99999", 3600)):
        def handler(request: httpx.Request, status=status, header=header) -> httpx.Response:
            return httpx.Response(status, request=request, headers={"Retry-After": header}, json={"message": "busy"})

        provider, client = _provider(handler)
        try:
            with pytest.raises(NotificationProviderError) as exc_info:
                _send(provider)
        finally:
            asyncio.run(client.aclose())
        error = exc_info.value
        assert error.outcome == "retryable_failure"
        assert error.retry_after_seconds == expected


def test_409_is_ambiguous_and_other_4xx_is_permanent() -> None:
    for status, expected in ((409, "ambiguous_outcome"), (422, "permanent_rejection")):
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, request=request, json={"message": "rejected"})

        provider, client = _provider(handler)
        try:
            with pytest.raises(NotificationProviderError) as exc_info:
                _send(provider)
        finally:
            asyncio.run(client.aclose())
        assert exc_info.value.outcome == expected


def test_success_without_provider_reference_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={})

    provider, client = _provider(handler)
    try:
        with pytest.raises(NotificationProviderError) as exc_info:
            _send(provider)
    finally:
        asyncio.run(client.aclose())
    assert exc_info.value.error_code == "provider_reference_missing"
    assert exc_info.value.outcome == "ambiguous_outcome"


def test_disabled_or_insecure_provider_fails_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request, json={"id": "should_not_send"})

    client = httpx.AsyncClient(base_url="https://api.resend.test", transport=httpx.MockTransport(handler))
    provider = ResendEmailProvider(
        mode="disabled",
        api_key="",
        from_email="members@doers.example",
        base_url="https://api.resend.test",
        client=client,
    )
    try:
        with pytest.raises(NotificationProviderError) as exc_info:
            _send(provider)
    finally:
        asyncio.run(client.aclose())
    assert exc_info.value.error_code == "provider_disabled"
    assert calls == 0


def test_reconciliation_returns_evidence_for_terminal_and_nonterminal_states() -> None:
    for event in ("sent", "delivery_delayed", "delivered", "bounced", "complained"):
        def handler(request: httpx.Request, event=event) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(200, request=request, json={"id": "email_123", "last_event": event})

        provider, client = _provider(handler)
        try:
            evidence = asyncio.run(provider.reconcile("email_123"))
        finally:
            asyncio.run(client.aclose())
        assert evidence.provider_reference_id == "email_123"
        assert evidence.last_event == event
        assert len(evidence.provider_evidence_sha256) == 64


def test_reconciliation_unknown_event_never_becomes_terminal_truth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"id": "email_123", "last_event": "mystery"})

    provider, client = _provider(handler)
    try:
        with pytest.raises(NotificationProviderError) as exc_info:
            asyncio.run(provider.reconcile("email_123"))
    finally:
        asyncio.run(client.aclose())
    assert exc_info.value.outcome == "retryable_failure"
    assert exc_info.value.error_code == "reconciliation_last_event_unknown"


def test_request_hash_changes_with_authoritative_destination_or_projection() -> None:
    original = ResendEmailProvider.request_sha256(
        destination="a@example.test",
        member_name="Member",
        template_key="branch_lifecycle_status_changed",
        template_data=DATA,
        from_email="members@doers.example",
    )
    moved = ResendEmailProvider.request_sha256(
        destination="b@example.test",
        member_name="Member",
        template_key="branch_lifecycle_status_changed",
        template_data=DATA,
        from_email="members@doers.example",
    )
    changed = ResendEmailProvider.request_sha256(
        destination="a@example.test",
        member_name="Member",
        template_key="branch_lifecycle_status_changed",
        template_data={**DATA, "to_status": "active"},
        from_email="members@doers.example",
    )
    assert original != moved
    assert original != changed
