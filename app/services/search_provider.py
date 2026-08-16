from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from app.observability.search_metrics import record_provider_call


_PROVIDER_CODE = "opensearch"
_ALLOWED_FAILURE_OUTCOMES = {
    "provider_accepted_nonterminal",
    "permanent_rejection",
    "retryable_failure",
    "ambiguous_outcome",
}
_REPAIRABLE_DRIFT_CODES = {
    "provider_document_mismatch",
    "provider_version_ahead",
    "delete_not_proven",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


@dataclass(frozen=True)
class SearchEffectEvidence:
    provider_code: str
    provider_index: str
    provider_document_id: str
    request_sha256: str
    provider_version: int | None
    provider_evidence_sha256: str
    document_sha256: str | None


class SearchProviderError(RuntimeError):
    """A classified downstream result that is safe to persist as P4B evidence."""

    def __init__(
        self,
        message: str,
        *,
        outcome: str,
        error_code: str,
        request_sha256: str,
        provider_index: str | None = None,
        provider_document_id: str | None = None,
        provider_version: int | None = None,
        provider_evidence_sha256: str | None = None,
        document_sha256: str | None = None,
    ) -> None:
        if outcome not in _ALLOWED_FAILURE_OUTCOMES:
            raise ValueError(f"Unsupported search outcome: {outcome!r}")
        super().__init__(message)
        self.outcome = outcome
        self.error_code = error_code[:160]
        self.request_sha256 = request_sha256
        self.provider_index = provider_index
        self.provider_document_id = provider_document_id
        self.provider_version = provider_version
        self.provider_evidence_sha256 = provider_evidence_sha256
        self.document_sha256 = document_sha256

    @property
    def is_repairable_drift(self) -> bool:
        return (
            self.error_code in _REPAIRABLE_DRIFT_CODES
            and self.provider_index is not None
            and self.provider_document_id is not None
            and self.provider_version is not None
            and self.provider_evidence_sha256 is not None
        )


class OpenSearchProvider:
    """REST adapter whose success is based on provider state, not HTTP hope.

    PostgreSQL owns the desired projection version. Index operations use strict
    ``version_type=external`` so an equal or older version can never replace
    different content. Delete operations use ``external_gte``: equality is safe
    for the same desired absence and makes retries of an already-applied delete
    idempotent, while any newer provider version still wins and is fenced as
    drift. Every accepted mutation is followed by a real-time provider readback.
    """

    def __init__(
        self,
        *,
        mode: str,
        base_url: str,
        index: str,
        username: str = "",
        password: str = "",
        timeout_seconds: float = 5.0,
        verify_tls: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.mode = mode.strip().lower()
        self.base_url = base_url.strip().rstrip("/")
        self.index = index.strip()
        self.username = username
        self.password = password
        self.timeout_seconds = float(timeout_seconds)
        self.verify_tls = bool(verify_tls)
        self._client = client

    @classmethod
    def from_settings(cls, config: Any | None = None) -> "OpenSearchProvider":
        if config is None:
            # Lazy import keeps deterministic unit tests independent of process
            # profile/database settings construction.
            from app.core.config import settings as config

        return cls(
            mode=str(getattr(config, "SEARCH_PROVIDER_MODE", "disabled")),
            base_url=str(getattr(config, "OPENSEARCH_URL", "")),
            index=str(getattr(config, "OPENSEARCH_INDEX", "branches-v1")),
            username=str(getattr(config, "OPENSEARCH_USERNAME", "")),
            password=str(getattr(config, "OPENSEARCH_PASSWORD", "")),
            timeout_seconds=float(
                getattr(config, "OPENSEARCH_TIMEOUT_SECONDS", 5.0)
            ),
            verify_tls=bool(getattr(config, "OPENSEARCH_VERIFY_TLS", True)),
        )

    @staticmethod
    def request_sha256(
        *,
        index: str,
        branch_id: str,
        operation: str,
        desired_version: int,
        document: Mapping[str, Any] | None,
    ) -> str:
        return _sha256_json(
            {
                "provider": _PROVIDER_CODE,
                "index": index,
                "document_id": branch_id,
                "operation": operation,
                "desired_version": desired_version,
                "document": document,
            }
        )

    def _request_hash(
        self,
        *,
        branch_id: str,
        operation: str,
        desired_version: int,
        document: Mapping[str, Any] | None,
    ) -> str:
        return self.request_sha256(
            index=self.index,
            branch_id=branch_id,
            operation=operation,
            desired_version=desired_version,
            document=document,
        )

    def _validate_configuration(self, request_sha256: str) -> None:
        if self.mode != _PROVIDER_CODE:
            raise SearchProviderError(
                "Search provider mode is not configured for OpenSearch",
                outcome="permanent_rejection",
                error_code="provider_disabled",
                request_sha256=request_sha256,
            )
        if not self.base_url or not self.index:
            raise SearchProviderError(
                "OpenSearch URL and index must be configured",
                outcome="permanent_rejection",
                error_code="provider_configuration_missing",
                request_sha256=request_sha256,
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise SearchProviderError(
                "OpenSearch timeout must be in the range (0, 60] seconds",
                outcome="permanent_rejection",
                error_code="provider_timeout_configuration_invalid",
                request_sha256=request_sha256,
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise SearchProviderError(
                "OpenSearch URL must use HTTP or HTTPS",
                outcome="permanent_rejection",
                error_code="provider_url_invalid",
                request_sha256=request_sha256,
            )

    def _client_kwargs(self) -> dict[str, Any]:
        auth: httpx.BasicAuth | None = None
        if self.username or self.password:
            if not self.username or not self.password:
                raise ValueError("OpenSearch basic auth requires both username and password")
            auth = httpx.BasicAuth(self.username, self.password)
        return {
            "base_url": self.base_url,
            "timeout": httpx.Timeout(self.timeout_seconds),
            "verify": self.verify_tls,
            "auth": auth,
            "follow_redirects": False,
        }

    async def apply(
        self,
        *,
        branch_id: str,
        operation: str,
        desired_version: int,
        document: Mapping[str, Any] | None,
    ) -> SearchEffectEvidence:
        if operation not in {"index", "delete"}:
            raise ValueError(f"Unsupported search operation: {operation!r}")
        if desired_version < 1:
            raise ValueError("Search desired_version must be positive")
        if operation == "index" and document is None:
            raise ValueError("Index operation requires a document")
        if operation == "delete" and document is not None:
            raise ValueError("Delete operation must not carry a document")

        request_sha256 = self._request_hash(
            branch_id=branch_id,
            operation=operation,
            desired_version=desired_version,
            document=document,
        )
        started = time.monotonic()
        try:
            self._validate_configuration(request_sha256)
            if self._client is not None:
                evidence = await self._apply_with_client(
                    self._client,
                    branch_id=branch_id,
                    operation=operation,
                    desired_version=desired_version,
                    document=document,
                    request_sha256=request_sha256,
                )
            else:
                async with httpx.AsyncClient(**self._client_kwargs()) as client:
                    evidence = await self._apply_with_client(
                        client,
                        branch_id=branch_id,
                        operation=operation,
                        desired_version=desired_version,
                        document=document,
                        request_sha256=request_sha256,
                    )
        except SearchProviderError as exc:
            record_provider_call(
                operation=operation,
                outcome=exc.outcome,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise
        except ValueError as exc:
            classified = SearchProviderError(
                str(exc),
                outcome="permanent_rejection",
                error_code="provider_configuration_invalid",
                request_sha256=request_sha256,
            )
            record_provider_call(
                operation=operation,
                outcome=classified.outcome,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise classified from exc

        record_provider_call(
            operation=operation,
            outcome="definite_success",
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return evidence

    async def _apply_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        branch_id: str,
        operation: str,
        desired_version: int,
        document: Mapping[str, Any] | None,
        request_sha256: str,
    ) -> SearchEffectEvidence:
        index_path = quote(self.index, safe="")
        document_id = str(branch_id)
        document_path = quote(document_id, safe="")
        path = f"/{index_path}/_doc/{document_path}"
        params = {
            "version": str(desired_version),
            "version_type": "external_gte" if operation == "delete" else "external",
            "refresh": "wait_for",
        }

        mutation_response: httpx.Response | None = None
        mutation_error: Exception | None = None
        try:
            if operation == "index":
                mutation_response = await client.put(path, params=params, json=document)
            else:
                mutation_response = await client.delete(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            mutation_error = exc

        # A transport failure has an unknown commit point. A successful or
        # conflicting mutation can also be a retry of work already applied.
        # In all of those cases, provider state is the authority for success.
        should_verify = mutation_error is not None
        if mutation_response is not None:
            should_verify = (
                200 <= mutation_response.status_code < 300
                or mutation_response.status_code in {404, 409}
            )

        if should_verify:
            try:
                evidence = await self._verify_provider_state(
                    client,
                    path=path,
                    branch_id=document_id,
                    operation=operation,
                    desired_version=desired_version,
                    document=document,
                    request_sha256=request_sha256,
                    mutation_response=mutation_response,
                )
            except SearchProviderError as verification_error:
                if mutation_error is not None:
                    raise SearchProviderError(
                        "OpenSearch mutation outcome remained ambiguous after verification",
                        outcome="ambiguous_outcome",
                        error_code="mutation_transport_ambiguous",
                        request_sha256=request_sha256,
                    ) from mutation_error
                raise verification_error
            return evidence

        if mutation_response is None:
            raise SearchProviderError(
                "OpenSearch mutation failed before a response was available",
                outcome="ambiguous_outcome",
                error_code="mutation_transport_ambiguous",
                request_sha256=request_sha256,
            ) from mutation_error

        status = mutation_response.status_code
        if status == 202:
            outcome = "provider_accepted_nonterminal"
            code = "provider_accepted_nonterminal"
        elif status == 429 or status >= 500:
            outcome = "retryable_failure"
            code = f"provider_http_{status}"
        else:
            outcome = "permanent_rejection"
            code = f"provider_http_{status}"
        raise SearchProviderError(
            f"OpenSearch mutation returned HTTP {status}",
            outcome=outcome,
            error_code=code,
            request_sha256=request_sha256,
        )

    async def _verify_provider_state(
        self,
        client: httpx.AsyncClient,
        *,
        path: str,
        branch_id: str,
        operation: str,
        desired_version: int,
        document: Mapping[str, Any] | None,
        request_sha256: str,
        mutation_response: httpx.Response | None,
    ) -> SearchEffectEvidence:
        try:
            response = await client.get(path, params={"realtime": "true"})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SearchProviderError(
                "OpenSearch verification request failed",
                outcome="ambiguous_outcome",
                error_code="verification_transport_ambiguous",
                request_sha256=request_sha256,
            ) from exc

        mutation_body = self._safe_json(mutation_response)
        get_body = self._safe_json(response)
        evidence_payload = {
            "operation": operation,
            "desired_version": desired_version,
            "mutation_status": None
            if mutation_response is None
            else mutation_response.status_code,
            "mutation_body": mutation_body,
            "get_status": response.status_code,
            "get_body": get_body,
        }
        provider_evidence_sha256 = _sha256_json(evidence_payload)
        desired_document_sha256 = (
            _sha256_json(document) if operation == "index" else None
        )

        if operation == "delete":
            if response.status_code == 404 or (
                response.status_code == 200
                and isinstance(get_body, dict)
                and get_body.get("found") is False
            ):
                provider_version = self._provider_version(mutation_body, fallback=None)
                if provider_version is None:
                    raise SearchProviderError(
                        "OpenSearch absence was observed without mutation version evidence",
                        outcome="ambiguous_outcome",
                        error_code="delete_version_unproven",
                        request_sha256=request_sha256,
                    )
                if provider_version < desired_version:
                    raise SearchProviderError(
                        "OpenSearch delete version is behind the desired version",
                        outcome="retryable_failure",
                        error_code="provider_version_behind",
                        request_sha256=request_sha256,
                    )
                if provider_version > desired_version:
                    raise SearchProviderError(
                        "OpenSearch delete version is ahead of the authoritative clock",
                        outcome="permanent_rejection",
                        error_code="provider_version_ahead",
                        request_sha256=request_sha256,
                        provider_index=self.index,
                        provider_document_id=branch_id,
                        provider_version=provider_version,
                        provider_evidence_sha256=provider_evidence_sha256,
                    )
                return SearchEffectEvidence(
                    provider_code=_PROVIDER_CODE,
                    provider_index=self.index,
                    provider_document_id=branch_id,
                    request_sha256=request_sha256,
                    provider_version=provider_version,
                    provider_evidence_sha256=provider_evidence_sha256,
                    document_sha256=None,
                )
            if response.status_code in {429} or response.status_code >= 500:
                raise SearchProviderError(
                    f"OpenSearch delete verification returned HTTP {response.status_code}",
                    outcome="retryable_failure",
                    error_code=f"verification_http_{response.status_code}",
                    request_sha256=request_sha256,
                )
            observed_version = self._provider_version(get_body, fallback=None)
            raise SearchProviderError(
                "OpenSearch still contains the document after delete",
                outcome="permanent_rejection"
                if response.status_code == 200
                else "ambiguous_outcome",
                error_code="delete_not_proven",
                request_sha256=request_sha256,
                provider_index=self.index if observed_version is not None else None,
                provider_document_id=branch_id if observed_version is not None else None,
                provider_version=observed_version,
                provider_evidence_sha256=(
                    provider_evidence_sha256 if observed_version is not None else None
                ),
            )

        if response.status_code != 200 or not isinstance(get_body, dict):
            if response.status_code in {404, 429} or response.status_code >= 500:
                outcome = "retryable_failure"
            else:
                outcome = "permanent_rejection"
            raise SearchProviderError(
                f"OpenSearch index verification returned HTTP {response.status_code}",
                outcome=outcome,
                error_code=f"verification_http_{response.status_code}",
                request_sha256=request_sha256,
            )

        if get_body.get("found") is not True:
            raise SearchProviderError(
                "OpenSearch did not prove the indexed document exists",
                outcome="retryable_failure",
                error_code="index_not_found_after_write",
                request_sha256=request_sha256,
            )
        source = get_body.get("_source")
        if not isinstance(source, dict):
            raise SearchProviderError(
                "OpenSearch response omitted a usable _source document",
                outcome="permanent_rejection",
                error_code="provider_source_missing",
                request_sha256=request_sha256,
            )
        provider_version = self._provider_version(get_body, fallback=None)
        if provider_version is None:
            raise SearchProviderError(
                "OpenSearch response omitted provider version evidence",
                outcome="permanent_rejection",
                error_code="provider_version_missing",
                request_sha256=request_sha256,
            )
        if provider_version < desired_version:
            raise SearchProviderError(
                "OpenSearch provider version is behind the desired version",
                outcome="retryable_failure",
                error_code="provider_version_behind",
                request_sha256=request_sha256,
            )
        if provider_version > desired_version:
            raise SearchProviderError(
                "OpenSearch provider version is ahead of the authoritative clock",
                outcome="permanent_rejection",
                error_code="provider_version_ahead",
                request_sha256=request_sha256,
                provider_index=self.index,
                provider_document_id=branch_id,
                provider_version=provider_version,
                provider_evidence_sha256=provider_evidence_sha256,
                document_sha256=desired_document_sha256,
            )
        if _sha256_json(source) != desired_document_sha256:
            raise SearchProviderError(
                "OpenSearch provider document differs from the authoritative projection",
                outcome="permanent_rejection",
                error_code="provider_document_mismatch",
                request_sha256=request_sha256,
                provider_index=self.index,
                provider_document_id=branch_id,
                provider_version=provider_version,
                provider_evidence_sha256=provider_evidence_sha256,
                document_sha256=desired_document_sha256,
            )

        return SearchEffectEvidence(
            provider_code=_PROVIDER_CODE,
            provider_index=self.index,
            provider_document_id=branch_id,
            request_sha256=request_sha256,
            provider_version=provider_version,
            provider_evidence_sha256=provider_evidence_sha256,
            document_sha256=desired_document_sha256,
        )

    @staticmethod
    def _safe_json(response: httpx.Response | None) -> Any:
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            return {"body_sha256": _sha256(response.content)}

    @staticmethod
    def _provider_version(value: Any, *, fallback: int | None) -> int | None:
        if isinstance(value, dict):
            version = value.get("_version")
            if isinstance(version, int) and version >= 0:
                return version
        return fallback
