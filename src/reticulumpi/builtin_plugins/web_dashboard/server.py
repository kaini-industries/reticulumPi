"""aiohttp application setup, routes, security middleware."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import time
from typing import TYPE_CHECKING, Any

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.auth import _normalize_ip
from reticulumpi.builtin_plugins.web_dashboard.assets import (
    read_static_bytes,
    read_static_text,
    render_template,
    shell_asset_urls,
    static_content_type,
)
from reticulumpi.builtin_plugins.web_dashboard.keys import (
    AUTH_TOKEN_KEY,
    LOCAL_API_KEY,
    PLUGIN_KEY,
    WS_COMPRESS_KEY,
    get_app_plugin,
)
from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
    record_tile_hit,
    record_tile_miss,
    record_tile_reject,
)
from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import (
    PNG_SIGNATURE,
    _TileLockLease,
    is_png_content_type,
    store_tile,
)

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

# The local-service bearer token is restricted to these read-only GET routes.
# Internal services (NomadNet pages, scripts) cannot mutate state or upgrade
# to WebSockets with this credential.
LOCALHOST_ALLOWED_PATHS = frozenset(
    {
        "/api/status",
        "/api/node",
        "/api/interfaces",
        "/api/version",
    }
)


def _request_is_secure(request: aiohttp.web.Request, plugin: Any | None = None) -> bool:
    """Return true for direct HTTPS or an explicitly trusted HTTPS proxy hop."""

    if request.scheme == "https":
        return True
    if plugin is None:
        try:
            plugin = get_app_plugin(request.app)
        except (KeyError, TypeError):
            return False
    config = getattr(plugin, "config", {})
    if not isinstance(config, dict):
        return False
    proxy = config.get("reverse_proxy", {})
    if not isinstance(proxy, dict) or not proxy.get("enabled", False):
        return False
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    if not isinstance(forwarded_proto, str) or forwarded_proto.strip().lower() != "https":
        return False
    try:
        remote = ipaddress.ip_address(_normalize_ip(request.remote or ""))
    except ValueError:
        return False
    for raw_network in proxy.get("trusted_networks", []):
        try:
            if remote in ipaddress.ip_network(raw_network, strict=False):
                return True
        except (TypeError, ValueError):
            # Configuration validation rejects these at startup. Keep request
            # handling fail closed if a test double or runtime mutation bypasses it.
            continue
    return False


_SENSITIVE_NO_STORE_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/password",
        "/auth/login",
        "/api/auth/logout",
        "/api/config",
        "/api/services/restart",
    }
)


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
        ],
        client_max_size=64 * 1024,
    )
    app[PLUGIN_KEY] = plugin
    app[WS_COMPRESS_KEY] = plugin.config.get("ws_compress", True)
    app.on_response_prepare.append(_on_response_prepare)

    # API and WebSocket routes
    setup_api_routes(app)
    setup_websocket_routes(app)

    # Package resources are served explicitly so installed wheels do not
    # depend on a source checkout or on ``__file__`` filesystem layout.
    app.router.add_get("/static/{asset_path:.*}", _serve_static)

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
    response = await handler(request)
    _apply_security_headers(request, response)
    return response


def _apply_security_headers(
    request: aiohttp.web.Request,
    response: aiohttp.web.StreamResponse,
) -> None:
    """Apply headers to normal responses and framework-generated errors alike."""
    from reticulumpi.builtin_plugins.web_dashboard.api import API_VERSION

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "script-src-attr 'none'; "
        "connect-src 'self' https://api.planespotters.net; "
        "worker-src 'self'; "
        "style-src 'self'; "
        "style-src-elem 'self'; "
        "style-src-attr 'none'; "
        "img-src 'self' data: https://*.tile.openstreetmap.org"
        " https://api.planespotters.net https://*.plnspttrs.net; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTS only over HTTPS — sending it on plain HTTP is meaningless and could
    # wedge a client if they later downgrade. No preload/includeSubDomains.
    if _request_is_secure(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        response.headers["Api-Version"] = API_VERSION
    if request.path in _SENSITIVE_NO_STORE_PATHS or request.path.startswith(
        "/api/services/restart/"
    ):
        response.headers["Cache-Control"] = "private, no-store"
    elif request.path.startswith("/api/") and response.status >= 400:
        response.headers["Cache-Control"] = "private, no-store"
    elif request.path.startswith("/static/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"


async def _on_response_prepare(
    request: aiohttp.web.Request,
    response: aiohttp.web.StreamResponse,
) -> None:
    """Ensure redirects and HTTP exceptions receive the security policy too."""
    _apply_security_headers(request, response)


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
            request[AUTH_TOKEN_KEY] = token
            if (
                plugin._auth.password_change_required
                and (path.startswith("/api/") or path.startswith("/ws/"))
                and path not in {"/api/auth/logout", "/api/auth/password", "/api/version"}
            ):
                raise aiohttp.web.HTTPPreconditionRequired(
                    text=(
                        '{"ok": false, "error": "Password change required", '
                        '"code": 428, "password_change_required": true}'
                    ),
                    content_type="application/json",
                )
            return await handler(request)

        # Local service access is deliberately narrower than a dashboard
        # session: loopback-only, read-only, and limited to a fixed route set.
        # It is checked *after* normal session auth so authenticated localhost
        # users can use every dashboard route.
        local_api = plugin.config.get("local_api", {})
        local_enabled = bool(local_api.get("enabled")) if isinstance(local_api, dict) else False
        local_token = getattr(plugin, "_local_api_token", None)
        normalized_remote = _normalize_ip(request.remote or "")
        if (
            local_enabled
            and local_token
            and normalized_remote in ("127.0.0.1", "::1")
            and request.method == "GET"
            and path in LOCALHOST_ALLOWED_PATHS
            and token
            and secrets.compare_digest(token, local_token)
        ):
            request[LOCAL_API_KEY] = True
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


async def _serve_static(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve a validated resource from the installed dashboard package."""
    path = request.match_info.get("asset_path", "")
    if path == "version.js":
        from reticulumpi import __version__

        return aiohttp.web.Response(
            text=f"var APP_VERSION = {json.dumps(__version__)};\n",
            content_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )
    try:
        body = read_static_bytes(path)
        content_type = static_content_type(path)
    except (FileNotFoundError, ValueError):
        raise aiohttp.web.HTTPNotFound()
    return aiohttp.web.Response(body=body, content_type=content_type)


async def _serve_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=render_template("login.html"),
        content_type="text/html",
        headers={"Cache-Control": "private, no-cache"},
    )


async def _serve_spectrum(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=render_template("spectrum.html"),
        content_type="text/html",
        headers={"Cache-Control": "private, no-cache"},
    )


async def _serve_sw(request: aiohttp.web.Request) -> aiohttp.web.Response:
    plugin = get_app_plugin(request.app)
    max_entries = int(plugin.config.get("tile_cache_entries", 5000))
    content = read_static_text("sw.js")
    from reticulumpi import __version__

    marker = "  /*__RPI_BUILT_ASSETS__*/"
    if marker not in content:
        raise aiohttp.web.HTTPInternalServerError(text="Invalid packaged service worker")
    asset_entries = ",\n".join(f"  {json.dumps(url)}" for url in shell_asset_urls())
    content = (
        content.replace(marker, asset_entries)
        .replace(
            "var MAX_ENTRIES = 5000;",
            f"var MAX_ENTRIES = {max_entries};",
        )
        .replace(
            "var SHELL_CACHE = 'rpi-shell-' + APP_VERSION;",
            f"var SHELL_CACHE = 'rpi-shell-v{__version__}';",
        )
    )
    return aiohttp.web.Response(
        text=content,
        content_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


async def _handle_tile_proxy(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /tiles/{z}/{x}/{y}.png — proxy OSM tiles with local disk cache."""
    plugin = get_app_plugin(request.app)
    session = getattr(plugin, "_tile_session", None)
    if not session:
        record_tile_reject("unavailable")
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
        record_tile_reject("invalid_request")
        raise aiohttp.web.HTTPBadRequest(text="Invalid tile coordinates")

    z = str(z_int)
    x = str(x_int)
    y = str(y_int)

    cache_dir = getattr(plugin, "_tile_cache_dir", "")
    tile_path = os.path.join(cache_dir, z, x, f"{y}.png")

    if os.path.isfile(tile_path):
        record_tile_hit()
        return aiohttp.web.FileResponse(
            tile_path,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    locks = getattr(plugin, "_tile_locks", None)
    if not isinstance(locks, dict):
        locks = plugin._tile_locks = {}

    async with _TileLockLease(locks, tile_path):
        # A concurrent request may have populated the file while this request
        # waited, so never fetch or account for the same tile twice.
        if os.path.isfile(tile_path):
            record_tile_hit()
            return aiohttp.web.FileResponse(
                tile_path,
                headers={"Cache-Control": "private, max-age=86400"},
            )

        record_tile_miss()
        upstream = getattr(plugin, "_tile_upstream", "")
        url = upstream.replace("{z}", z).replace("{x}", x).replace("{y}", y)
        url = url.replace("{s}", "a")
        max_tile_bytes = getattr(plugin, "_tile_max_tile_bytes", 512_000)
        if not isinstance(max_tile_bytes, int) or max_tile_bytes <= 0:
            max_tile_bytes = 512_000

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    record_tile_reject("upstream")
                    raise aiohttp.web.HTTPBadGateway(text=f"Upstream returned {resp.status}")
                if not is_png_content_type(resp.headers.get("Content-Type", "")):
                    record_tile_reject("invalid_content")
                    raise aiohttp.web.HTTPBadGateway(text="Upstream tile is not image/png")
                data = await resp.content.read(max_tile_bytes + 1)
                if not isinstance(data, (bytes, bytearray)):
                    # Response-like test doubles and alternate clients may
                    # expose only ``read()``; enforce the same post-read cap.
                    data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            record_tile_reject("upstream")
            raise aiohttp.web.HTTPGatewayTimeout(text="Upstream tile fetch failed")

        if len(data) > max_tile_bytes:
            record_tile_reject("oversize")
            raise aiohttp.web.HTTPBadGateway(text="Upstream tile exceeds configured size limit")
        if not data.startswith(PNG_SIGNATURE):
            record_tile_reject("invalid_content")
            raise aiohttp.web.HTTPBadGateway(text="Upstream returned invalid tile content")

        try:
            if not await store_tile(plugin, tile_path, bytes(data)):
                record_tile_reject("capacity")
        except OSError:
            record_tile_reject("write_error")

    return aiohttp.web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


async def _serve_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    plugin = get_app_plugin(request.app)
    if plugin._auth.password_change_required and request.query.get("password_change") != "required":
        raise aiohttp.web.HTTPFound("/?password_change=required")
    html = render_template(
        "index.html",
        ready_features=_ready_dashboard_features(plugin),
    )
    tp = plugin.config.get("tile_proxy", {})
    if tp.get("enabled"):
        html = html.replace(
            "</head>",
            '<meta name="rpi-tile-url" content="/tiles/{z}/{x}/{y}.png">\n</head>',
        )
    return aiohttp.web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "private, no-cache"},
    )


def _ready_dashboard_features(plugin: Any) -> frozenset[str]:
    """Resolve built-in feature visibility before the browser's first paint."""

    core = getattr(plugin, "app", None)
    plugins = getattr(core, "plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}

    def ready(name: str) -> bool:
        getter = getattr(type(core), "get_ready_plugin", None)
        if callable(getter):
            return getter(core, name) is not None
        instance = plugins.get(name)
        state = getattr(instance, "plugin_state", None)
        value = getattr(state, "value", state)
        if value is not None:
            return value == "ready"
        return bool(getattr(instance, "_active", False))

    dependencies = {
        "messages": ("messaging_hub",),
        "adsb": ("adsb_radar",),
        "space": ("space_tracker",),
        "radio": ("fm_receiver",),
        "mesh": ("network_map", "mesh_telemetry"),
        "routing": ("connectivity_monitor",),
        "mesh-bridge": ("mesh_bridge",),
        "meshtastic": ("meshtastic_gateway",),
        "meshcore": ("meshcore_gateway", "meshcore_observer"),
        "gps": ("gps_telemetry",),
        "ntp": ("ntp_server",),
        "link-tester": ("lora_link_tester",),
        "hotspot": ("hotspot_monitor", "captive_portal"),
        "weather-alert": ("weather_alert",),
        "ais": ("ais_receiver",),
        "acars": ("acars_decoder",),
        "radiosonde": ("radiosonde_tracker",),
        "noaa": ("noaa_apt_decoder",),
        "map": (
            "meshtastic_gateway",
            "meshcore_gateway",
            "meshcore_observer",
            "node_location_tracker",
            "gps_telemetry",
            "mesh_telemetry",
        ),
    }
    return frozenset(
        feature
        for feature, providers in dependencies.items()
        if any(ready(provider) for provider in providers)
    )
