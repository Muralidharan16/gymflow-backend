from __future__ import annotations

import asyncio

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


def test_provider_clock_ahead_is_drift_not_false_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, request=request, json={"error": "conflict"})
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 9,
                "found": True,
                "_source": DOCUMENT,
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
    assert exc_info.value.error_code == "provider_version_ahead"


def test_delete_timeout_plus_absence_is_not_enough_to_invent_version_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ReadTimeout("unknown delete commit point", request=request)
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
                    operation="delete",
                    desired_version=8,
                    document=None,
                )
            )
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.outcome == "ambiguous_outcome"
    assert exc_info.value.error_code == "mutation_transport_ambiguous"


def test_delete_absence_without_provider_version_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(
                404,
                request=request,
                json={"_id": BRANCH_ID, "result": "not_found"},
            )
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
                    operation="delete",
                    desired_version=8,
                    document=None,
                )
            )
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.outcome == "ambiguous_outcome"
    assert exc_info.value.error_code == "delete_version_unproven"


def test_redirect_is_not_followed_or_treated_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://unexpected.example.test"},
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
    assert exc_info.value.error_code == "provider_http_302"
