"""Disposable local OpenSearch-shaped HTTP process for the P5-D runtime gate.

This is a process/network test double, not an in-process mock.  State is stored
on disk so killing and restarting the HTTP process preserves accepted provider
versions and the logical mutation count used by recovery assertions.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


class _Store:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.lock = threading.Lock()
        if not self.state_path.exists():
            self._write({"documents": {}, "mutation_count": 0})

    def _read(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            value = {"documents": {}, "mutation_count": 0}
        if not isinstance(value, dict):
            raise RuntimeError("P5-D provider state must be a JSON object")
        value.setdefault("documents", {})
        value.setdefault("mutation_count", 0)
        return value

    def _write(self, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            dir=self.state_path.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def get(self, document_id: str) -> dict | None:
        with self.lock:
            return self._read()["documents"].get(document_id)

    def put(self, document_id: str, version: int, source: dict) -> tuple[int, dict]:
        with self.lock:
            state = self._read()
            existing = state["documents"].get(document_id)
            current_version = int(existing["version"]) if existing else 0
            if version <= current_version:
                return HTTPStatus.CONFLICT, {
                    "_version": current_version,
                    "result": "version_conflict",
                }
            state["documents"][document_id] = {
                "version": version,
                "source": source,
                "deleted": False,
            }
            state["mutation_count"] = int(state["mutation_count"]) + 1
            self._write(state)
            return HTTPStatus.CREATED, {"_version": version, "result": "created"}

    def delete(self, document_id: str, version: int) -> tuple[int, dict]:
        with self.lock:
            state = self._read()
            existing = state["documents"].get(document_id)
            current_version = int(existing["version"]) if existing else 0
            if version < current_version:
                return HTTPStatus.CONFLICT, {
                    "_version": current_version,
                    "result": "version_conflict",
                }
            if existing and version == current_version and bool(existing.get("deleted")):
                return HTTPStatus.NOT_FOUND, {
                    "_version": current_version,
                    "result": "not_found",
                }
            state["documents"][document_id] = {
                "version": version,
                "source": None,
                "deleted": True,
            }
            state["mutation_count"] = int(state["mutation_count"]) + 1
            self._write(state)
            return HTTPStatus.OK, {"_version": version, "result": "deleted"}


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, format: str, *args: object) -> None:
        print("P5D_PROVIDER " + format % args, flush=True)

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _document_id(self) -> str:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 3 or parts[1] != "_doc":
            raise ValueError("unsupported OpenSearch-shaped path")
        return parts[2]

    def _version(self) -> int:
        query = parse_qs(urlsplit(self.path).query)
        raw = query.get("version", [""])[0]
        version = int(raw)
        if version < 1:
            raise ValueError("version must be positive")
        return version

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        try:
            document_id = self._document_id()
        except ValueError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        record = self.server.store.get(document_id)
        if not record or bool(record.get("deleted")):
            payload = {"found": False}
            if record:
                payload["_version"] = int(record["version"])
            self._json(HTTPStatus.NOT_FOUND, payload)
            return
        self._json(
            HTTPStatus.OK,
            {
                "found": True,
                "_version": int(record["version"]),
                "_source": record["source"],
            },
        )

    def do_PUT(self) -> None:  # noqa: N802
        try:
            document_id = self._document_id()
            version = self._version()
            length = int(self.headers.get("Content-Length", "0"))
            source = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(source, dict):
                raise ValueError("document must be an object")
            status, body = self.server.store.put(document_id, version, source)
            self._json(status, body)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            document_id = self._document_id()
            version = self._version()
            status, body = self.server.store.delete(document_id, version)
            self._json(status, body)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


class _Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: _Store) -> None:
        super().__init__(address, _Handler)
        self.store = store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True, dest="state_path")
    args = parser.parse_args()
    if args.port < 1024 or args.port > 65535:
        raise SystemExit("P5-D provider port must be an unprivileged TCP port")
    store = _Store(args.state_path.resolve())
    server = _Server(("127.0.0.1", args.port), store)
    print(
        "P5D_PROVIDER_READY="
        + json.dumps({"port": args.port, "state_path": str(store.state_path)}, sort_keys=True),
        flush=True,
    )
    server.serve_forever(poll_interval=0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
