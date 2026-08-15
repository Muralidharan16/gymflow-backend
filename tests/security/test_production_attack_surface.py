import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.core.control_plane import (
    INTERNAL_CONTROL_HEADER,
    PRESTOP_PATH,
    InternalControlPlaneMiddleware,
    internal_control_client_is_loopback,
    internal_control_token_matches,
)
from app.core.drain import PodDrainCoordinator


ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _request(
    method: str,
    *,
    token: str | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    headers = []
    if token is not None:
        headers.append((INTERNAL_CONTROL_HEADER.lower().encode("ascii"), token.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": PRESTOP_PATH,
            "raw_path": PRESTOP_PATH.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 12345),
            "server": ("127.0.0.1", 8000),
        }
    )


async def _must_not_reach_inner(_request: Request):
    raise AssertionError("internal control request reached the user middleware stack")


def _install_control_stubs(monkeypatch, *, expected_token: str):
    config_module = types.ModuleType("app.core.config")
    config_module.settings = SimpleNamespace(INTERNAL_CONTROL_TOKEN=expected_token)
    monkeypatch.setitem(sys.modules, "app.core.config", config_module)

    class StubDrainCoordinator:
        calls = 0

        async def trigger_drain(self):
            self.calls += 1

    drain_module = types.ModuleType("app.core.drain")
    drain_module.drain_coordinator = StubDrainCoordinator()
    monkeypatch.setitem(sys.modules, "app.core.drain", drain_module)
    return drain_module.drain_coordinator


def test_internal_control_token_never_accepts_missing_or_empty_values() -> None:
    assert not internal_control_token_matches(None, None)
    assert not internal_control_token_matches("", "")
    assert not internal_control_token_matches("present", "")
    assert not internal_control_token_matches("", "present")
    assert not internal_control_token_matches("wrong", "correct")
    assert internal_control_token_matches("same-secret", "same-secret")


def test_internal_control_client_must_be_loopback_ip() -> None:
    assert internal_control_client_is_loopback("127.0.0.1")
    assert internal_control_client_is_loopback("::1")
    assert not internal_control_client_is_loopback(None)
    assert not internal_control_client_is_loopback("10.0.0.10")
    assert not internal_control_client_is_loopback("localhost")


def test_internal_control_rejects_get_before_user_middleware() -> None:
    middleware = InternalControlPlaneMiddleware(lambda *_args, **_kwargs: None)
    response = asyncio.run(middleware.dispatch(_request("GET"), _must_not_reach_inner))

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["cache-control"] == "no-store"


def test_internal_control_rejects_bad_token_without_triggering_drain(monkeypatch) -> None:
    expected = "a" * 48
    drain = _install_control_stubs(monkeypatch, expected_token=expected)
    middleware = InternalControlPlaneMiddleware(lambda *_args, **_kwargs: None)

    response = asyncio.run(
        middleware.dispatch(
            _request("POST", token="b" * 48),
            _must_not_reach_inner,
        )
    )

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert drain.calls == 0


def test_internal_control_rejects_remote_client_even_with_valid_token(monkeypatch) -> None:
    expected = "c" * 48
    drain = _install_control_stubs(monkeypatch, expected_token=expected)
    middleware = InternalControlPlaneMiddleware(lambda *_args, **_kwargs: None)

    response = asyncio.run(
        middleware.dispatch(
            _request("POST", token=expected, client_host="10.1.2.3"),
            _must_not_reach_inner,
        )
    )

    assert response.status_code == 403
    assert drain.calls == 0


def test_internal_control_accepts_exact_token_from_loopback_and_triggers_drain_once(monkeypatch) -> None:
    expected = "d" * 48
    drain = _install_control_stubs(monkeypatch, expected_token=expected)
    middleware = InternalControlPlaneMiddleware(lambda *_args, **_kwargs: None)

    response = asyncio.run(
        middleware.dispatch(
            _request("POST", token=expected),
            _must_not_reach_inner,
        )
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {"status": "drained"}
    assert drain.calls == 1


def test_drain_coordinator_is_terminal_after_shutdown() -> None:
    coordinator = PodDrainCoordinator(drain_window_seconds=0)
    asyncio.run(coordinator.trigger_drain())
    assert coordinator.status == "SHUTDOWN"

    asyncio.run(coordinator.trigger_drain())
    assert coordinator.status == "SHUTDOWN"


def test_main_has_no_user_route_for_prestop_and_installs_control_plane_outermost() -> None:
    source = _source("app/main.py")

    assert '@app.get("/_system/preStop")' not in source
    assert '@app.post("/_system/preStop")' not in source
    control_registration = source.index("app.add_middleware(InternalControlPlaneMiddleware)")
    cors_registration = source.index("app.add_middleware(\n    CORSMiddleware,")
    assert control_registration > cors_registration


def test_mock_s3_route_is_registered_only_inside_development_guard() -> None:
    source = _source("app/routers/assets.py")
    guard = 'if settings.ENVIRONMENT == "development":\n'
    route = '    @router.post("/mock-s3/upload", include_in_schema=False)\n'

    assert guard in source
    assert route in source
    assert source.index(guard) < source.index(route)
    assert '@router.post("/mock-s3/upload")' not in source


def test_production_configuration_is_fail_closed_for_control_and_web_origins() -> None:
    config_source = _source("app/core/config.py")
    settings_source = _source("app/core/settings_schema.py")
    compose_source = _source("deploy/docker-compose.production-identities.yml")

    assert "INTERNAL_CONTROL_TOKEN: str = \"\"" in settings_source
    assert "validate_production_security_boundaries" in config_source
    assert 'host == "*"' in config_source
    assert 'host == "localhost" or host.endswith(".localhost")' in config_source
    assert 'address.is_loopback or address.is_unspecified' in config_source
    assert '"FRONTEND_URL",\n            self.FRONTEND_URL' in config_source
    assert '"BACKEND_BASE_URL",\n            self.BACKEND_BASE_URL' in config_source
    assert "CORS_ORIGINS must explicitly include FRONTEND_URL in production" in config_source
    assert '_validate_production_secret("SECRET_KEY", self.SECRET_KEY)' in config_source
    assert '"INTERNAL_CONTROL_TOKEN",\n                self.INTERNAL_CONTROL_TOKEN' in config_source
    assert "INTERNAL_CONTROL_TOKEN must be distinct from SECRET_KEY" in config_source
    assert "${INTERNAL_CONTROL_TOKEN:?" in compose_source
