"""Run one real P5-E worker poll against a durable local provider double.

The provider store is a SQLite file owned by the certification controller.  It
survives replacement Python processes and enforces the same logical search
version or notification idempotency key across retries.  PostgreSQL remains the
only application authority; this store models only downstream provider truth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx


_INDEX = "branches-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS search_effects (
                logical_key TEXT PRIMARY KEY,
                provider_version INTEGER NOT NULL,
                document_json TEXT,
                deleted INTEGER NOT NULL CHECK (deleted IN (0,1)),
                effect_count INTEGER NOT NULL CHECK (effect_count >= 0),
                mutation_calls INTEGER NOT NULL CHECK (mutation_calls >= 0)
            );
            CREATE TABLE IF NOT EXISTS notification_effects (
                idempotency_key TEXT PRIMARY KEY,
                reference_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                effect_count INTEGER NOT NULL CHECK (effect_count >= 0),
                send_calls INTEGER NOT NULL CHECK (send_calls >= 0)
            );
            """
        )


def seed_search_document(
    path: Path,
    *,
    document_id: str,
    provider_version: int,
    document: dict[str, Any],
) -> None:
    initialize_store(path)
    logical_key = f"{_INDEX}/{document_id}"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO search_effects(
                logical_key,provider_version,document_json,deleted,
                effect_count,mutation_calls
            ) VALUES (?,?,?,0,0,0)
            """,
            (
                logical_key,
                provider_version,
                _canonical_json(document),
            ),
        )


class DurableProviderTransport:
    """Minimal provider protocol with durable idempotency and effect counts."""

    def __init__(self, store: Path) -> None:
        self.store = store
        initialize_store(store)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = [part for part in request.url.path.split("/") if part]
        if len(parts) == 3 and parts[1] == "_doc":
            return self._search(request, index=parts[0], document_id=parts[2])
        if request.method == "POST" and request.url.path == "/emails":
            return self._notification(request)
        return httpx.Response(404, json={"error": "unsupported test-provider route"})

    def _search(
        self,
        request: httpx.Request,
        *,
        index: str,
        document_id: str,
    ) -> httpx.Response:
        logical_key = f"{index}/{document_id}"
        with sqlite3.connect(self.store) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT provider_version,document_json,deleted,effect_count,
                       mutation_calls
                FROM search_effects WHERE logical_key=?
                """,
                (logical_key,),
            ).fetchone()

            if request.method == "GET":
                if row is None or int(row[2]) == 1:
                    return httpx.Response(
                        404,
                        json={"_id": document_id, "found": False},
                    )
                return httpx.Response(
                    200,
                    json={
                        "_id": document_id,
                        "_version": int(row[0]),
                        "found": True,
                        "_source": json.loads(str(row[1])),
                    },
                )

            try:
                desired_version = int(request.url.params["version"])
            except (KeyError, ValueError) as exc:
                raise AssertionError("search mutation omitted provider version") from exc

            if request.method == "PUT":
                document = json.loads(request.content.decode("utf-8"))
                document_json = _canonical_json(document)
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO search_effects(
                            logical_key,provider_version,document_json,deleted,
                            effect_count,mutation_calls
                        ) VALUES (?,?,?,0,1,1)
                        """,
                        (logical_key, desired_version, document_json),
                    )
                    return httpx.Response(
                        201,
                        json={"_id": document_id, "_version": desired_version},
                    )

                connection.execute(
                    """
                    UPDATE search_effects
                    SET mutation_calls=mutation_calls+1
                    WHERE logical_key=?
                    """,
                    (logical_key,),
                )
                current_version = int(row[0])
                same_effect = (
                    current_version == desired_version
                    and int(row[2]) == 0
                    and str(row[1]) == document_json
                )
                if same_effect or desired_version <= current_version:
                    return httpx.Response(
                        409,
                        json={
                            "_id": document_id,
                            "_version": current_version,
                            "error": "version_conflict_engine_exception",
                        },
                    )
                connection.execute(
                    """
                    UPDATE search_effects
                    SET provider_version=?,document_json=?,deleted=0,
                        effect_count=effect_count+1
                    WHERE logical_key=?
                    """,
                    (desired_version, document_json, logical_key),
                )
                return httpx.Response(
                    200,
                    json={"_id": document_id, "_version": desired_version},
                )

            if request.method == "DELETE":
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO search_effects(
                            logical_key,provider_version,document_json,deleted,
                            effect_count,mutation_calls
                        ) VALUES (?,?,NULL,1,1,1)
                        """,
                        (logical_key, desired_version),
                    )
                else:
                    current_version = int(row[0])
                    if desired_version < current_version:
                        connection.execute(
                            """
                            UPDATE search_effects
                            SET mutation_calls=mutation_calls+1
                            WHERE logical_key=?
                            """,
                            (logical_key,),
                        )
                        return httpx.Response(
                            409,
                            json={
                                "_id": document_id,
                                "_version": current_version,
                                "error": "version_conflict_engine_exception",
                            },
                        )
                    repeats_tombstone = (
                        int(row[2]) == 1 and current_version == desired_version
                    )
                    connection.execute(
                        """
                        UPDATE search_effects
                        SET provider_version=?,document_json=NULL,deleted=1,
                            effect_count=effect_count+?,
                            mutation_calls=mutation_calls+1
                        WHERE logical_key=?
                        """,
                        (
                            desired_version,
                            0 if repeats_tombstone else 1,
                            logical_key,
                        ),
                    )
                return httpx.Response(
                    200,
                    json={"_id": document_id, "_version": desired_version},
                )

        return httpx.Response(405, json={"error": "unsupported search method"})

    def _notification(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return httpx.Response(400, json={"error": "missing idempotency key"})
        request_sha256 = _sha256(_canonical_json(json.loads(request.content)))
        with sqlite3.connect(self.store) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT reference_id,request_sha256
                FROM notification_effects WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()
            if row is None:
                reference_id = f"p5e-{_sha256(key)[:24]}"
                connection.execute(
                    """
                    INSERT INTO notification_effects(
                        idempotency_key,reference_id,request_sha256,
                        effect_count,send_calls
                    ) VALUES (?,?,?,1,1)
                    """,
                    (key, reference_id, request_sha256),
                )
            else:
                reference_id = str(row[0])
                connection.execute(
                    """
                    UPDATE notification_effects
                    SET send_calls=send_calls+1
                    WHERE idempotency_key=?
                    """,
                    (key,),
                )
                if str(row[1]) != request_sha256:
                    return httpx.Response(409, json={"error": "idempotency conflict"})
            return httpx.Response(200, json={"id": reference_id})


async def _run(store: Path) -> dict[str, int]:
    from app.services.notification_delivery_service import ResendEmailProvider
    from app.services.search_provider import OpenSearchProvider
    from app.tasks import branch_outbox_poller

    transport = httpx.MockTransport(DurableProviderTransport(store))
    search_client = httpx.AsyncClient(
        base_url="https://p5e-provider.invalid",
        transport=transport,
    )
    notification_client = httpx.AsyncClient(
        base_url="https://p5e-provider.invalid",
        transport=transport,
    )
    search_provider = OpenSearchProvider(
        mode="opensearch",
        base_url="https://p5e-provider.invalid",
        index=_INDEX,
        client=search_client,
        metrics_required=False,
        environment="p5e-certification",
    )
    notification_provider = ResendEmailProvider(
        mode="resend",
        api_key="p5e-test-provider-key",
        from_email="p5e@example.test",
        base_url="https://p5e-provider.invalid",
        client=notification_client,
        metrics_required=False,
        environment="p5e-certification",
    )

    OpenSearchProvider.from_settings = classmethod(  # type: ignore[method-assign]
        lambda cls, config=None: search_provider
    )
    ResendEmailProvider.from_settings = classmethod(  # type: ignore[method-assign]
        lambda cls, config=None: notification_provider
    )
    try:
        return await branch_outbox_poller._poll_outbox()
    finally:
        await search_client.aclose()
        await notification_client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    arguments = parser.parse_args()
    if os.environ.get("P5E_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-E provider fault runner requires explicit CI enablement")
    store = arguments.store.resolve()
    initialize_store(store)
    result = asyncio.run(_run(store))
    print(_canonical_json({"marker": "P5E_WORKER_RESULT", "summary": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
