from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.api_runtime import (
    DrainAdmissionMiddleware,
    PreStopConfigurationError,
    SystemControlMiddleware,
    authorize_prestop_token,
)
from app.core.drain import PodDrainCoordinator


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app/main.py").read_text(encoding="utf-8")
RUNTIME_SOURCE = (ROOT / "app/core/api_runtime.py").read_text(encoding="utf-8")
DRAIN_SOURCE = (ROOT / "app/core/drain.py").read_text(encoding="utf-8")


def test_prestop_secret_fails_closed_and_uses_exact_match() -> None:
    with pytest.raises(PreStopConfigurationError):
        authorize_prestop_token("anything", expected_token=None, production=True)
    with pytest.raises(PreStopConfigurationError):
        authorize_prestop_token("anything", expected_token="short", production=True)

    expected = "a" * 48
    assert authorize_prestop_token(expected, expected_token=expected, production=True) is True
    assert authorize_prestop_token("b" * 48, expected_token=expected, production=True) is False
    assert "secrets.compare_digest" in RUNTIME_SOURCE
    assert "DOERS_PRESTOP_CONTROL_TOKEN" in RUNTIME_SOURCE


@pytest.mark.asyncio
async def test_atomic_admission_rejects_new_work_after_drain_begins() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0, hard_timeout_seconds=1)
    assert await coordinator.try_admit_request() is True
    assert coordinator.inflight_count == 1

    drain_task = asyncio.create_task(coordinator.trigger_drain())
    await asyncio.sleep(0)
    assert coordinator.status == "DRAINING"
    assert await coordinator.try_admit_request() is False
    assert coordinator.inflight_count == 1

    await coordinator.release_request()
    await drain_task
    assert coordinator.status == "SHUTDOWN"
    assert coordinator.inflight_count == 0


@pytest.mark.asyncio
async def test_repeated_drain_calls_share_one_bounded_sequence() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0.01, hard_timeout_seconds=1)
    assert await coordinator.try_admit_request() is True

    first = asyncio.create_task(coordinator.trigger_drain())
    await asyncio.sleep(0)
    second = asyncio.create_task(coordinator.trigger_drain())
    await asyncio.sleep(0)

    assert coordinator.status == "DRAINING"
    assert coordinator.inflight_count == 1
    await coordinator.release_request()
    await asyncio.gather(first, second)
    assert coordinator.status == "SHUTDOWN"
    assert coordinator.inflight_count == 0
    assert "asyncio.shield(task)" in DRAIN_SOURCE


@pytest.mark.asyncio
async def test_release_never_underflows_inflight_counter() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0, hard_timeout_seconds=0)
    await coordinator.release_request()
    await coordinator.release_request()
    assert coordinator.inflight_count == 0


@pytest.mark.asyncio
async def test_drain_admission_middleware_counts_only_admitted_business_requests() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0, hard_timeout_seconds=0)

    async def business(_: Request):
        assert coordinator.inflight_count == 1
        return JSONResponse({"ok": True})

    async def system(_: Request):
        return JSONResponse({"system": True})

    app = Starlette(
        routes=[
            Route("/business", business),
            Route("/_system/live", system),
        ]
    )
    app.add_middleware(DrainAdmissionMiddleware, coordinator=coordinator)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/business")
        assert response.status_code == 200
        assert coordinator.inflight_count == 0

        await coordinator.trigger_drain()
        rejected = await client.get("/business")
        assert rejected.status_code == 503
        assert rejected.json()["reason"] == "pod_draining"
        assert coordinator.inflight_count == 0

        system_response = await client.get("/_system/live")
        assert system_response.status_code == 200
        assert coordinator.inflight_count == 0


@pytest.mark.asyncio
async def test_liveness_stays_up_while_readiness_fails_immediately_on_drain() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0, hard_timeout_seconds=1)
    assert await coordinator.try_admit_request() is True
    drain_task = asyncio.create_task(coordinator.trigger_drain())
    await asyncio.sleep(0)
    assert coordinator.status == "DRAINING"

    app = Starlette()
    app.add_middleware(SystemControlMiddleware, coordinator=coordinator)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        live = await client.get("/_system/live")
        ready = await client.get("/_system/ready")
        legacy_ready = await client.get("/health")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        assert ready.status_code == 503
        assert ready.json()["reason"] == "draining"
        assert legacy_ready.status_code == 503

    await coordinator.release_request()
    await drain_task


def test_main_uses_outer_system_control_and_p7_admission_stack() -> None:
    assert "app.add_middleware(SystemControlMiddleware)" in MAIN_SOURCE
    assert "app.add_middleware(DrainAdmissionMiddleware)" in MAIN_SOURCE
    assert "app.add_middleware(P7SecurityHeadersMiddleware)" in MAIN_SOURCE
    assert "app.add_middleware(SecurityHeadersMiddleware)" not in MAIN_SOURCE
    assert "EXEMPT_PATHS.update(SYSTEM_REQUEST_PATHS)" in MAIN_SOURCE

    # The old directly exposed route must not remain as a bypass around P7 control.
    assert '@app.get("/_system/preStop")' not in MAIN_SOURCE
    assert '@app.post("/_system/preStop")' not in MAIN_SOURCE
    assert 'request.headers.get(PRESTOP_HEADER_NAME)' in RUNTIME_SOURCE
    assert "await self._coordinator.trigger_drain()" in RUNTIME_SOURCE


def test_system_control_paths_bypass_business_middleware_before_admission() -> None:
    assert "path not in SYSTEM_REQUEST_PATHS" in RUNTIME_SOURCE
    assert "return await call_next(request)" in RUNTIME_SOURCE
    assert RUNTIME_SOURCE.index("class SystemControlMiddleware") < RUNTIME_SOURCE.index(
        "class DrainAdmissionMiddleware"
    )
    # Registration is reversed: the last-added system control middleware is outermost.
    assert MAIN_SOURCE.rindex("app.add_middleware(SystemControlMiddleware)") > MAIN_SOURCE.rindex(
        "app.add_middleware(DrainAdmissionMiddleware)"
    )
