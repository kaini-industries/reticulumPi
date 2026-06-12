"""aiohttp application setup, routes, security middleware."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import tempfile
import time
from typing import TYPE_CHECKING

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.auth import _normalize_ip

if TYPE_CHECKING:
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

log = logging.getLogger(__name__)

# Paths that do not require authentication
PUBLIC_PATHS = frozenset(
    {
        "/login.html",
        "/api/auth/login",
        "/auth/login",
        "/api/version",
        "/sw.js",
    }
)

# Static file prefixes that are public. NOTE: /tiles/ is intentionally NOT
# public — map tile requests are same-origin <img> loads that carry the
# SameSite=Lax session cookie, so the auth middleware lets logged-in clients
# through while denying anonymous use of the node as an open OSM proxy.
PUBLIC_PREFIXES = ("/static/",)


def create_app(plugin: WebDashboardPlugin) -> aiohttp.web.Application:
    """Build and return the aiohttp Application with all routes and middleware."""
    from reticulumpi.builtin_plugins.web_dashboard.api import setup_api_routes
    from reticulumpi.builtin_plugins.web_dashboard.websocket_handler import (
        setup_websocket_routes,
    )

    app = aiohttp.web.Application(
        middlewares=[
            ip_allowlist_middleware_factory(plugin),
            compression_middleware,
            security_headers_middleware,
            auth_middleware_factory(plugin),
        ]
    )
    app["plugin"] = plugin
    app["ws_compress"] = plugin.config.get("ws_compress", True)

    # API and WebSocket routes
    setup_api_routes(app)
    setup_websocket_routes(app)

    # Static files and root redirect
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_static("/static/", static_dir, show_index=False)

    # Serve login.html and index.html directly
    app.router.add_get("/login.html", _serve_login)
    app.router.add_get("/spectrum.html", _serve_spectrum)
    app.router.add_get("/sw.js", _serve_sw)
    app.router.add_get("/", _serve_index)
    app.router.add_get("/index.html", _serve_index)

    # Tile proxy (opt-in)
    if plugin.config.get("tile_proxy", {}).get("enabled"):
        app.router.add_get("/tiles/{z}/{x}/{y}.png", _handle_tile_proxy)

    return app


def ip_allowlist_middleware_factory(plugin: WebDashboardPlugin):
    """Create middleware that restricts access to configured CIDR networks.

    Ships dark: ``allowed_networks`` defaults to ``[]`` which allows all
    remotes. When populated, requests from non-member addresses get a 404
    (minimal signal to scanners) and a throttled WARNING. CIDRs are parsed
    once here at factory time; malformed entries are logged and skipped.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in plugin.config.get("allowed_networks", []) or []:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log.warning("Ignoring malformed allowed_networks entry: %r", cidr)

    deny_log_state: dict[str, float] = {}
    _DENY_LOG_INTERVAL = 10.0

    def _log_denied(ip: str) -> None:
        now = time.monotonic()
        last = deny_log_state.get(ip)
        if last is not None and now - last < _DENY_LOG_INTERVAL:
            return
        if len(deny_log_state) >= 10_000:
            oldest_ip = min(deny_log_state, key=lambda k: deny_log_state[k])
            del deny_log_state[oldest_ip]
        deny_log_state[ip] = now
        log.warning("Denied request from %s (not in allowed_networks)", ip)

    @aiohttp.web.middleware
    async def ip_allowlist_middleware(
        request: aiohttp.web.Request,
        handler,
    ) -> aiohttp.web.StreamResponse:
        if not networks:
            return await handler(request)

        remote = _normalize_ip(request.remote or "")
        try:
            addr = ipaddress.ip_address(remote)
        except ValueError:
            _log_denied(remote or "<unknown>")
            raise aiohttp.web.HTTPNotFound()

        if any(addr in net for net in networks):
            return await handler(request)

        _log_denied(remote)
        raise aiohttp.web.HTTPNotFound()

    return ip_allowlist_middleware


_COMPRESSIBLE = frozenset(
    {
        "text/html",
        "text/css",
        "application/javascript",
        "application/json",
        "text/plain",
        "image/svg+xml",
    }
)


_ZLIB_EXECUTOR_THRESHOLD = 32768


@aiohttp.web.middleware
async def compression_middleware(
    request: aiohttp.web.Request,
    handler,
) -> aiohttp.web.StreamResponse:
    """Enable gzip/deflate for compressible responses."""
    response = await handler(request)
    ct = response.content_type or ""
    if ct in _COMPRESSIBLE and "Content-Encoding" not in response.headers:
        response.enable_compression()
        if (response.content_length or 0) > _ZLIB_EXECUTOR_THRESHOLD:
            response.zlib_executor_size = _ZLIB_EXECUTOR_THRESHOLD
    return response


@aiohttp.web.middleware
async def security_headers_middleware(
    request: aiohttp.web.Request,
    handler,
) -> aiohttp.web.StreamResponse:
    """Add security headers and API version to all responses."""
    from reticulumpi.builtin_plugins.web_dashboard.api import API_VERSION

    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self' ws: wss: https://api.planespotters.net; "
        "worker-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://api.planespotters.net https://*.plnspttrs.net"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTS only over HTTPS — sending it on plain HTTP is meaningless and could
    # wedge a client if they later downgrade. No preload/includeSubDomains.
    if request.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        response.headers["Api-Version"] = API_VERSION
    return response


def auth_middleware_factory(plugin: WebDashboardPlugin):
    """Create authentication middleware that checks session tokens."""

    @aiohttp.web.middleware
    async def auth_middleware(
        request: aiohttp.web.Request,
        handler,
    ) -> aiohttp.web.StreamResponse:
        path = request.path

        # Allow public paths
        if path in PUBLIC_PATHS:
            return await handler(request)
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await handler(request)

        # Allow localhost requests for internal services (NomadNet pages, scripts)
        if plugin.config.get("allow_localhost_api", False) and request.remote in (
            "127.0.0.1",
            "::1",
        ):
            if request.method in ("POST", "PUT", "DELETE"):
                if not request.headers.get("X-Requested-With"):
                    raise aiohttp.web.HTTPForbidden(
                        text='{"ok": false, "error": "Missing X-Requested-With header", "code": 403}',
                        content_type="application/json",
                    )
            return await handler(request)

        # Extract token from Authorization header or cookie
        token = _extract_token(request)
        if token and plugin._auth.validate_token(token):
            # CSRF defense-in-depth: state-changing requests from browsers
            # must include a custom header that cross-origin forms cannot set.
            if request.method in ("POST", "PUT", "DELETE"):
                if not request.headers.get("X-Requested-With"):
                    raise aiohttp.web.HTTPForbidden(
                        text='{"ok": false, "error": "Missing X-Requested-With header", "code": 403}',
                        content_type="application/json",
                    )
            request["token"] = token
            return await handler(request)

        # Not authenticated
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise aiohttp.web.HTTPFound("/login.html")
        raise aiohttp.web.HTTPUnauthorized(
            text='{"ok": false, "error": "Authentication required", "code": 401}',
            content_type="application/json",
        )

    return auth_middleware


def _extract_token(request: aiohttp.web.Request) -> str | None:
    """Extract bearer token from Authorization header or session cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    return request.cookies.get("session")


async def _serve_login(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return aiohttp.web.FileResponse(os.path.join(static_dir, "login.html"))


async def _serve_spectrum(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return aiohttp.web.FileResponse(os.path.join(static_dir, "spectrum.html"))


async def _serve_sw(request: aiohttp.web.Request) -> aiohttp.web.Response:
    plugin = request.app["plugin"]
    max_entries = int(plugin.config.get("tile_cache_entries", 5000))
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    with open(os.path.join(static_dir, "sw.js")) as f:
        content = f.read()
    from reticulumpi import __version__

    content = content.replace(
        "var MAX_ENTRIES = 5000;",
        f"var MAX_ENTRIES = {max_entries};",
    ).replace(
        "var SHELL_CACHE = 'rpi-shell-v1';",
        f"var SHELL_CACHE = 'rpi-shell-v{__version__}';",
    )
    return aiohttp.web.Response(
        text=content,
        content_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


async def _handle_tile_proxy(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /tiles/{z}/{x}/{y}.png — proxy OSM tiles with local disk cache."""
    plugin = request.app["plugin"]
    session = getattr(plugin, "_tile_session", None)
    if not session:
        raise aiohttp.web.HTTPServiceUnavailable(text="Tile proxy not initialised")

    try:
        z_int = int(request.match_info["z"])
        x_int = int(request.match_info["x"])
        y_int = int(request.match_info["y"])
        if not (0 <= z_int <= 19):
            raise ValueError
        max_coord = (1 << z_int) - 1
        if not (0 <= x_int <= max_coord) or not (0 <= y_int <= max_coord):
            raise ValueError
    except ValueError:
        raise aiohttp.web.HTTPBadRequest(text="Invalid tile coordinates")

    z = str(z_int)
    x = str(x_int)
    y = str(y_int)

    cache_dir = getattr(plugin, "_tile_cache_dir", "")
    tile_path = os.path.join(cache_dir, z, x, f"{y}.png")

    if os.path.isfile(tile_path):
        return aiohttp.web.FileResponse(
            tile_path,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    upstream = getattr(plugin, "_tile_upstream", "")
    url = upstream.replace("{z}", z).replace("{x}", x).replace("{y}", y)
    url = url.replace("{s}", "a")

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise aiohttp.web.HTTPBadGateway(text=f"Upstream returned {resp.status}")
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise aiohttp.web.HTTPGatewayTimeout(text="Upstream tile fetch failed")

    max_bytes = getattr(plugin, "_tile_max_bytes", 0)
    cur_bytes = getattr(plugin, "_tile_cache_bytes", 0)
    if max_bytes <= 0 or cur_bytes + len(data) <= max_bytes:
        tile_dir = os.path.dirname(tile_path)
        os.makedirs(tile_dir, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=tile_dir, suffix=".tmp")
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp_path, tile_path)
            plugin._tile_cache_bytes = getattr(plugin, "_tile_cache_bytes", 0) + len(data)
        except OSError:
            pass

    return aiohttp.web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _serve_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    plugin = request.app["plugin"]
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    tp = plugin.config.get("tile_proxy", {})
    if tp.get("enabled"):
        with open(os.path.join(static_dir, "index.html")) as f:
            html = f.read()
        html = html.replace(
            "</head>",
            '<meta name="rpi-tile-url" content="/tiles/{z}/{x}/{y}.png">\n</head>',
        )
        return aiohttp.web.Response(text=html, content_type="text/html")
    return aiohttp.web.FileResponse(os.path.join(static_dir, "index.html"))
