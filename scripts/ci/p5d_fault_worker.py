"""Fresh-process production poller entry point for the P5-D runtime gate.

The controller owns destructive dependency/connection fault injection.  This
module intentionally contains no production monkeypatches: each invocation
imports and runs the real branch-outbox poller using the reduced worker binding.
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse


def _guard() -> None:
    if os.environ.get("P5D_PROCESS_FAULTS") != "1":
        raise RuntimeError("P5-D process faults require explicit CI enablement")
    raw = os.environ.get("WORKER_DATABASE_URL", "")
    parsed = urlparse(raw)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("P5-D worker database must be local")
    database = (parsed.path or "").lstrip("/")
    expected = os.environ.get("P5D_DISPOSABLE_DATABASE", "")
    if not expected or database != expected or "test" not in database:
        raise RuntimeError("P5-D worker database is not the acknowledged disposable database")


def main() -> int:
    _guard()
    from app.tasks import branch_outbox_poller

    result = asyncio.run(branch_outbox_poller._poll_outbox())
    print("P5D_WORKER_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
