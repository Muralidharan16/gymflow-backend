from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.services.search_provider import OpenSearchProvider, SearchProviderError


BRANCH_ID = "44000000-0000-4000-8000-000000000011"
DOCUMENT = {
    "branch_id": BRANCH_ID,
    "organization_id": "44000000-0000-4000-8000-000000000001",
    "name": "P4B Search Branch",
    "slug": "p4b-search-branch",
    "timezone": "Asia/Kolkata",
    "region_code": "PY",
    "country_code": "IN",
    "status": "active",
    "is_operational": True,
    "is_public": True,
    "search_version": 7,
}


def _provider(handler) -> tuple[OpenSearchProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://search.example.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        OpenSearchProvider(
            mode="opensearch",
            base_url="https://search.example.test",
            index="branches-v1",
            timeout_seconds=2,
            client=client,
        ),
        client,
    )


def test_index_success_requires_real_time_provider_readback() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PUT":
            assert request.url.params["version"] == "7"
            assert request.url.params["version_type"] == "external"
            assert request.url.params["refresh"] == "wait_for"
            return httpx.Response(
                201,
                request=request,
                json={"_id": BRANCH_ID, "_version": 7, "result": "created"},
            )
        assert request.method == "GET"
        assert request.url.params["realtime"] == "true"
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 7,
                "found": True,
                "_source": DOCUMENT,
            },
        )

    provider, client = _provider(handler)
    try:
        evidence = asyncio.run(
            provider.apply(
                branch_id=BRANCH_ID,
                operation="index",
                desired_version=7,
                document=DOCUMENT,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert seen == [
        ("PUT", f"/branches-v1/_doc/{BRANCH_ID}"),
        ("GET", f"/branches-v1/_doc/{BRANCH_ID}"),
    ]
    assert evidence.provider_code == "opensearch"
    assert evidence.provider_index == "branches-v1"
    assert evidence.provider_document_id == BRANCH_ID
    assert evidence.provider_version == 7
    assert len(evidence.request_sha256) == 64
    assert len(evidence.provider_evidence_sha256) == 64
    assert evidence.document_sha256 is not None
    assert len(evidence.document_sha256) == 64


def test_equal_external_version_conflict_is_idempotent_only_if_get_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(
                409,
                request=request,
                json={"error": {"type": "version_conflict_engine_exception"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 7,
                "found": True,
                "_source": DOCUMENT,
            },
        )

    provider, client = _provider(handler)
    try:
        evidence = asyncio.run(
            provider.apply(
                branch_id=BRANCH_ID,
                operation="index",
                desired_version=7,
                document=DOCUMENT,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert evidence.provider_version == 7


def test_equal_external_version_conflict_rejects_different_provider_document() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, request=request, json={"error": "conflict"})
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 7,
                "found": True,
                "_source": {**DOCUMENT, "name": "drifted"},
            },
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(SearchProviderError) as exc_info:
            asyncio.run(
                provider.apply(
                    branch_id=BRANCH_ID,
                    operation="index",
                    desired_version=7,
                    document=DOCUMENT,
                )
            )
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.outcome == "permanent_rejection"
    assert exc_info.value.error_code == "provider_document_mismatch"


def test_timeout_is_recovered_only_when_real_time_get_proves_index_state() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method == "PUT":
            raise httpx.ReadTimeout("unknown commit point", request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 7,
                "found": True,
                "_source": DOCUMENT,
            },
        )

    provider, client = _provider(handler)
    try:
        evidence = asyncio.run(
            provider.apply(
                branch_id=BRANCH_ID,
                operation="index",
                desired_version=7,
                document=DOCUMENT,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert calls == 2
    assert evidence.provider_version == 7


def test_timeout_without_provider_proof_remains_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ReadTimeout("unknown commit point", request=request)
        return httpx.Response(
            404,
            request=request,
            json={"_id": BRANCH_ID, "found": False},
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(SearchProviderError) as exc_info:
            asyncio.run(
                provider.apply(
                    branch_id=BRANCH_ID,
                    operation="index",
                    desired_version=7,
                    document=DOCUMENT,
                )
            )
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.outcome == "ambiguous_outcome"
    assert exc_info.value.error_code == "mutation_transport_ambiguous"


def test_delete_success_requires_provider_absence_and_idempotent_versioning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.params["version"] == "8"
            assert request.url.params["version_type"] == "external_gte"
            assert request.url.params["refresh"] == "wait_for"
            return httpx.Response(
                200,
                request=request,
                json={"_id": BRANCH_ID, "_version": 8, "result": "deleted"},
            )
        return httpx.Response(
            404,
            request=request,
            json={"_id": BRANCH_ID, "found": False},
        )

    provider, client = _provider(handler)
    try:
        evidence = asyncio.run(
            provider.apply(
                branch_id=BRANCH_ID,
                operation="delete",
                desired_version=8,
                document=None,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert evidence.provider_version == 8
    assert evidence.document_sha256 is None
    assert len(evidence.provider_evidence_sha256) == 64


def test_delete_at_older_version_cannot_remove_newer_provider_document() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.params["version_type"] == "external_gte"
            return httpx.Response(409, request=request, json={"error": "conflict"})
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 9,
                "found": True,
                "_source": {**DOCUMENT, "search_version": 9},
            },
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(SearchProviderError) as exc_info:
            asyncio.run(
                provider.apply(
                    branch_id=BRANCH_ID,
                    operation="delete",
                    desired_version=8,
                    document=None,
                )
            )
    finally:
        asyncio.run(client.aclose())

    error = exc_info.value
    assert error.error_code == "delete_not_proven"
    assert error.is_repairable_drift
    assert error.provider_version == 9


def test_provider_503_is_retryable_and_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"error": "unavailable"})

    provider, client = _provider(handler)
    try:
        with pytest.raises(SearchProviderError) as exc_info:
            asyncio.run(
                provider.apply(
                    branch_id=BRANCH_ID,
                    operation="index",
                    desired_version=7,
                    document=DOCUMENT,
                )
            )
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.outcome == "retryable_failure"
    assert exc_info.value.error_code == "provider_http_503"


def test_disabled_provider_fails_closed_with_persistable_request_hash() -> None:
    provider = OpenSearchProvider.from_settings(
        SimpleNamespace(
            SEARCH_PROVIDER_MODE="disabled",
            OPENSEARCH_URL="",
            OPENSEARCH_INDEX="branches-v1",
            OPENSEARCH_USERNAME="",
            OPENSEARCH_PASSWORD="",
            OPENSEARCH_TIMEOUT_SECONDS=5.0,
            OPENSEARCH_VERIFY_TLS=True,
        )
    )

    with pytest.raises(SearchProviderError) as exc_info:
        asyncio.run(
            provider.apply(
                branch_id=BRANCH_ID,
                operation="index",
                desired_version=7,
                document=DOCUMENT,
            )
        )

    assert exc_info.value.outcome == "permanent_rejection"
    assert exc_info.value.error_code == "provider_disabled"
    assert len(exc_info.value.request_sha256) == 64
