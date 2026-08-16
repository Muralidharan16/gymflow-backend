from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from app.services.search_provider import OpenSearchProvider, SearchProviderError


LIVE_URL = os.environ.get("OPENSEARCH_LIVE_URL", "").rstrip("/")
LIVE_INDEX = os.environ.get("P4B_LIVE_INDEX", "")
pytestmark = pytest.mark.skipif(
    not LIVE_URL or not LIVE_INDEX,
    reason="live OpenSearch endpoint is not configured",
)


def _provider() -> OpenSearchProvider:
    return OpenSearchProvider(
        mode="opensearch",
        base_url=LIVE_URL,
        index=LIVE_INDEX,
        timeout_seconds=5,
        verify_tls=False,
    )


def _document(branch_id: str, *, name: str, search_version: int) -> dict[str, object]:
    return {
        "branch_id": branch_id,
        "organization_id": "44000000-0000-4000-8000-000000000001",
        "name": name,
        "slug": f"live-{branch_id[-8:]}",
        "timezone": "Asia/Kolkata",
        "region_code": "PY",
        "country_code": "IN",
        "status": "active",
        "is_operational": True,
        "is_public": True,
        "search_version": search_version,
    }


async def _delete_live_index() -> None:
    async with httpx.AsyncClient(base_url=LIVE_URL, timeout=5) as client:
        response = await client.delete(f"/{LIVE_INDEX}")
        assert response.status_code in {200, 404}, response.text


def test_live_opensearch_external_version_idempotency_drift_delete_and_missing_repair() -> None:
    provider = _provider()
    asyncio.run(_delete_live_index())

    idempotent_id = str(uuid.uuid4())
    idempotent_doc = _document(idempotent_id, name="Idempotent", search_version=10)
    first = asyncio.run(
        provider.apply(
            branch_id=idempotent_id,
            operation="index",
            desired_version=10,
            document=idempotent_doc,
        )
    )
    assert first.provider_version == 10
    assert first.document_sha256 is not None

    duplicate = asyncio.run(
        provider.apply(
            branch_id=idempotent_id,
            operation="index",
            desired_version=10,
            document=idempotent_doc,
        )
    )
    assert duplicate.provider_version == 10
    assert duplicate.document_sha256 == first.document_sha256

    mismatch_id = str(uuid.uuid4())
    mismatch_doc = _document(mismatch_id, name="Authoritative", search_version=20)
    asyncio.run(
        provider.apply(
            branch_id=mismatch_id,
            operation="index",
            desired_version=20,
            document=mismatch_doc,
        )
    )
    with pytest.raises(SearchProviderError) as mismatch_info:
        asyncio.run(
            provider.apply(
                branch_id=mismatch_id,
                operation="index",
                desired_version=20,
                document={**mismatch_doc, "name": "Conflicting"},
            )
        )
    mismatch = mismatch_info.value
    assert mismatch.error_code == "provider_document_mismatch"
    assert mismatch.is_repairable_drift
    assert mismatch.provider_version == 20

    ahead_id = str(uuid.uuid4())
    ahead_doc = _document(ahead_id, name="Ahead", search_version=30)
    asyncio.run(
        provider.apply(
            branch_id=ahead_id,
            operation="index",
            desired_version=30,
            document=ahead_doc,
        )
    )
    with pytest.raises(SearchProviderError) as ahead_info:
        asyncio.run(
            provider.apply(
                branch_id=ahead_id,
                operation="index",
                desired_version=29,
                document={**ahead_doc, "search_version": 29},
            )
        )
    ahead = ahead_info.value
    assert ahead.error_code == "provider_version_ahead"
    assert ahead.is_repairable_drift
    assert ahead.provider_version == 30

    delete_id = str(uuid.uuid4())
    delete_doc = _document(delete_id, name="Delete", search_version=40)
    asyncio.run(
        provider.apply(
            branch_id=delete_id,
            operation="index",
            desired_version=40,
            document=delete_doc,
        )
    )
    deleted = asyncio.run(
        provider.apply(
            branch_id=delete_id,
            operation="delete",
            desired_version=41,
            document=None,
        )
    )
    assert deleted.provider_version == 41
    assert deleted.document_sha256 is None

    missing_id = str(uuid.uuid4())
    missing_doc = _document(missing_id, name="Missing Repair", search_version=50)
    asyncio.run(
        provider.apply(
            branch_id=missing_id,
            operation="index",
            desired_version=50,
            document=missing_doc,
        )
    )
    # Simulate provider-side loss independent of PostgreSQL. The next periodic
    # reconciliation command carries the same authoritative DB version and must
    # recreate the missing provider projection rather than fabricate local sync.
    asyncio.run(_delete_live_index())
    repaired = asyncio.run(
        provider.apply(
            branch_id=missing_id,
            operation="index",
            desired_version=50,
            document=missing_doc,
        )
    )
    assert repaired.provider_version == 50
    assert repaired.document_sha256 is not None

    asyncio.run(_delete_live_index())
