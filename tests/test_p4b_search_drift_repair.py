from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.services.search_provider import OpenSearchProvider, SearchProviderError


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "v07d8e9f0a36_p4b_search_provider_drift_repair.py"
POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
METRICS = ROOT / "app" / "observability" / "search_metrics.py"
U07 = ROOT / "alembic" / "versions" / "u07d8e9f0a35_p4b_search_external_evidence.py"

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


def test_same_version_document_drift_carries_repair_evidence() -> None:
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

    error = exc_info.value
    assert error.error_code == "provider_document_mismatch"
    assert error.is_repairable_drift
    assert error.provider_version == 7
    assert error.provider_index == "branches-v1"
    assert error.provider_document_id == BRANCH_ID
    assert error.provider_evidence_sha256 is not None
    assert len(error.provider_evidence_sha256) == 64
    assert error.document_sha256 is not None
    assert len(error.document_sha256) == 64


def test_provider_clock_ahead_carries_fenced_repair_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, request=request, json={"error": "conflict"})
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 12,
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

    error = exc_info.value
    assert error.error_code == "provider_version_ahead"
    assert error.is_repairable_drift
    assert error.provider_version == 12
    assert error.provider_evidence_sha256 is not None


def test_delete_still_present_carries_repairable_provider_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
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
    assert error.document_sha256 is None


def test_unknown_mutation_commit_point_never_enters_drift_repair() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ReadTimeout("unknown commit point", request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "_id": BRANCH_ID,
                "_version": 12,
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

    error = exc_info.value
    assert error.error_code == "mutation_transport_ambiguous"
    assert error.outcome == "ambiguous_outcome"
    assert not error.is_repairable_drift


def test_drift_repair_migration_is_fenced_worker_only_and_requeues_above_provider_clock() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "v07d8e9f0a36"' in source
    assert 'down_revision = "u07d8e9f0a35"' in source
    assert "CREATE FUNCTION app_secure.repair_branch_search_provider_drift" in source
    assert "SECURITY DEFINER" in source
    assert "SET row_security = on" in source
    assert "leased_until > pg_catalog.clock_timestamp()" in source
    assert "FOR UPDATE;" in source
    assert "p_provider_document_id IS DISTINCT FROM v_branch::text" in source
    assert "GREATEST(v_current_version, p_provider_version) + 1" in source
    assert "search_last_synced_at = NULL" in source
    assert "search_provider_drift_repair_requeued" in source
    assert "'source','search_provider_drift_repair'" in source
    assert "GRANT EXECUTE ON FUNCTION {_FUNCTION} TO worker_runtime" in source
    assert "leaked drift repair capability" in source
    for forbidden in (
        "GRANT SELECT ON TABLE",
        "GRANT INSERT ON TABLE",
        "GRANT UPDATE ON TABLE",
        "GRANT DELETE ON TABLE",
        "BYPASSRLS",
    ):
        assert forbidden not in source


def test_periodic_reconciliation_rechecks_provider_even_when_local_ack_matches() -> None:
    source = U07.read_text(encoding="utf-8")
    reconciliation = source.split(
        "CREATE FUNCTION app_secure.enqueue_branch_search_reconciliation", 1
    )[1].split("CREATE FUNCTION app_secure.bump_branch_search_version_from_state", 1)[0]
    assert "search_provider_ack_version IS DISTINCT FROM s.search_visibility_version" in reconciliation
    assert "search_provider_reconciled_at IS NULL" in reconciliation
    assert "search_provider_reconciled_at < pg_catalog.clock_timestamp() - INTERVAL '24 hours'" in reconciliation
    assert "INSERT INTO public.branch_outbox_events" in reconciliation
    assert "search_last_synced_at =" not in reconciliation


def test_search_worker_uses_drift_repair_before_generic_dead_letter_path() -> None:
    source = POLLER.read_text(encoding="utf-8")
    handler = source.split("async def _process_search_event", 1)[1].split(
        "async def _fail_event", 1
    )[0]
    assert "if exc.is_repairable_drift:" in handler
    assert "await _repair_search_drift(" in handler
    assert handler.index("if exc.is_repairable_drift:") < handler.index(
        "await _record_search_failure("
    )


def test_metrics_are_low_cardinality_provider_health_latency_and_repair_instruments() -> None:
    source = METRICS.read_text(encoding="utf-8")
    assert '"doers.search.provider.requests"' in source
    assert 'create_histogram(\n    "doers.search.provider.latency"' in source
    assert '"doers.search.reconciliation.drift_repairs"' in source
    for forbidden_label in (
        '"tenant_id"',
        '"branch_id"',
        '"document_id"',
        '"url"',
    ):
        assert forbidden_label not in source
