"""P7-O production-like orchestration runtime fixture.

This module is CI-only. It imports the real FastAPI application and adds one
ordinary slow route so the rollout gate can hold an already-admitted request
open across preStop. The route is exempt from tenant JWT only; it deliberately
remains outside SYSTEM_REQUEST_PATHS and therefore passes through P7 drain
admission/in-flight accounting exactly like ordinary application traffic.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import text as sa_text

from app.core.database import AsyncSessionLocal
from app.core.middleware import EXEMPT_PATHS
from app.main import app


SLOW_PATH = "/_p7/slow"
EXEMPT_PATHS.add(SLOW_PATH)


@app.get(SLOW_PATH)
async def p7o_slow_request() -> dict[str, object]:
    marker = Path(os.environ["P7O_SLOW_STARTED_FILE"])
    hold_seconds = float(os.environ.get("P7O_SLOW_SECONDS", "20"))

    async with AsyncSessionLocal() as session:
        backend_pid = int((await session.execute(sa_text("SELECT pg_backend_pid()"))).scalar_one())
        marker.write_text(str(backend_pid), encoding="utf-8")
        await asyncio.sleep(hold_seconds)
        assert int((await session.execute(sa_text("SELECT 1"))).scalar_one()) == 1

    return {"status": "completed", "db_roundtrip": True, "backend_pid": backend_pid}
