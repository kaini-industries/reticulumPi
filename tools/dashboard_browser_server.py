"""Loopback-only dashboard server used by the Playwright release gate."""

from __future__ import annotations

import os
import re
import secrets
from types import SimpleNamespace

from aiohttp import web

from reticulumpi import __version__
from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager
from reticulumpi.builtin_plugins.web_dashboard.server import create_app


_SW_FIXTURE_COOKIE = "reticulumpi_browser_sw_fixture"
_SW_FIXTURES: dict[str, dict[str, str | None]] = {}
_SW_VERSION_RE = re.compile(r"var SHELL_CACHE = 'rpi-shell-[^']+';")
_SAFE_VERSION_RE = re.compile(r"[A-Za-z0-9._+-]{1,80}")


class _BrowserTestCore:
    plugins: dict = {}
    _failed_plugins: list = []
    reticulum = None
    config: dict = {}

    @staticmethod
    def _get_version() -> str:
        return __version__

    @staticmethod
    def get_status() -> dict:
        return {"version": __version__, "plugins": {}, "failed_plugins": []}


async def _configure_service_worker_fixture(request: web.Request) -> web.Response:
    """Configure one browser context's service-worker update fixture."""

    try:
        payload = await request.json()
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="invalid fixture payload")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="invalid fixture payload")

    version = payload.get("version")
    failed_asset = payload.get("failed_asset")
    if not isinstance(version, str) or _SAFE_VERSION_RE.fullmatch(version) is None:
        raise web.HTTPBadRequest(text="invalid fixture version")
    if failed_asset is not None and (
        not isinstance(failed_asset, str)
        or not failed_asset.startswith("/static/")
        or "?" in failed_asset
        or "#" in failed_asset
    ):
        raise web.HTTPBadRequest(text="invalid failed asset")

    token = request.cookies.get(_SW_FIXTURE_COOKIE)
    if token not in _SW_FIXTURES:
        token = secrets.token_urlsafe(24)
    _SW_FIXTURES[token] = {"version": version, "failed_asset": failed_asset}
    response = web.json_response({"ok": True})
    response.set_cookie(
        _SW_FIXTURE_COOKIE,
        token,
        httponly=True,
        max_age=300,
        path="/",
        samesite="Strict",
    )
    return response


@web.middleware
async def _service_worker_fixture_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Serve deterministic update versions/failures to one isolated test context."""

    state = _SW_FIXTURES.get(request.cookies.get(_SW_FIXTURE_COOKIE, ""))
    if state and request.path == state["failed_asset"]:
        raise web.HTTPServiceUnavailable(text="injected shell asset failure")

    response = await handler(request)
    if state and request.path == "/sw.js" and isinstance(response, web.Response):
        source = response.text
        replacement = f"var SHELL_CACHE = 'rpi-shell-{state['version']}';"
        updated, replacements = _SW_VERSION_RE.subn(replacement, source, count=1)
        if replacements != 1:
            raise web.HTTPInternalServerError(text="service-worker fixture marker missing")
        response.text = updated
    return response


def main() -> None:
    """Serve real packaged resources/security middleware with fixture APIs intercepted by tests."""
    host = "127.0.0.1"
    port = int(os.environ.get("RETICULUMPI_BROWSER_TEST_PORT", "18765"))
    password = os.environ.get("RETICULUMPI_BROWSER_TEST_PASSWORD", "browser-fixture")
    plugin = SimpleNamespace(
        app=_BrowserTestCore(),
        config={
            "local_api": {"enabled": False},
            "ssl": {},
            "tile_proxy": {"enabled": False},
            "tile_cache_entries": 5000,
            "ws_compress": False,
        },
        # Cross-browser workers intentionally authenticate in parallel. Keep
        # the fixture above their aggregate session count so one scenario
        # cannot evict another scenario's cookie mid-navigation.
        _auth=AuthManager(
            plaintext_password=password,
            session_timeout=300,
            max_sessions=128,
        ),
        _local_api_token=None,
    )
    app = create_app(plugin)
    app.middlewares.append(_service_worker_fixture_middleware)
    app.router.add_post("/__test/service-worker", _configure_service_worker_fixture)
    # Browser tests provide deterministic HTTP fixtures and do not need the
    # background metrics/spectrum collectors or their external dependencies.
    app.on_startup.clear()
    app.on_shutdown.clear()
    web.run_app(app, host=host, port=port, handle_signals=True, print=None)


if __name__ == "__main__":
    main()
