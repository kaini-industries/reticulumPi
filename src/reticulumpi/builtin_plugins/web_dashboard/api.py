"""JSON API route handlers — core utilities, auth, and system endpoints.

Domain-specific handlers are in:
  - api_interfaces.py  (interface config management)
  - api_mesh.py        (mesh network, routing, transport, reachability)
  - api_services.py    (LoRa, messaging, NomadNet, Meshtastic, sensors, etc.)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import aiohttp.web

if TYPE_CHECKING:
    pass

SENSITIVE_KEYS = frozenset({
    "password", "password_hash", "token", "secret", "api_key",
    "private_key", "credentials", "auth_token",
})

# API version — bump when making breaking changes to response schemas.
# Included in all API responses via the Api-Version header so clients
# can detect incompatibilities before they parse the response body.
API_VERSION = "1.0"


# ── Shared utilities (imported by sub-modules) ───────────────────────


def _get_plugin_address(p) -> str | None:
    """Extract the RNS/LXMF address hash from a plugin, if it has one."""
    import RNS

    # LXMF plugins (message_echo, info_bot) store their destination here
    dest = getattr(p, "local_lxmf_destination", None)
    if dest is not None and hasattr(dest, "hash"):
        return RNS.prettyhexrep(dest.hash)

    # RNS destination plugins (heartbeat_announce, example_plugin)
    dest = getattr(p, "destination", None)
    if dest is not None and hasattr(dest, "hash"):
        return RNS.prettyhexrep(dest.hash)

    return None


def _collect_local_services(app) -> list[dict]:
    """Collect RNS/LXMF destinations from active plugins (local 0-hop services)."""
    import RNS

    services: list[dict] = []
    for name, p in app.plugins.items():
        dest = getattr(p, "local_lxmf_destination", None)
        if dest is None or not hasattr(dest, "hash"):
            dest = getattr(p, "destination", None)
        if dest is None or not hasattr(dest, "hash"):
            continue
        try:
            dest_hash = RNS.prettyhexrep(dest.hash)
        except Exception:
            continue

        # Parse app_name/aspects from dest.name ("app.aspect1.aspect2.hexhash")
        app_name, aspects = "", ""
        try:
            raw_name = getattr(dest, "name", "") or ""
            parts = raw_name.split(".") if raw_name else []
            if len(parts) >= 2:
                app_name = parts[0]
                # Last segment is identity hex hash; middle segments are aspects
                aspects = ".".join(parts[1:-1]) if len(parts) > 2 else parts[1]
        except Exception:
            pass

        services.append({
            "destination_hash": dest_hash,
            "plugin_name": getattr(p, "plugin_name", name),
            "app_name": app_name,
            "aspects": aspects,
            "is_local": True,
        })
    return services


def _build_traffic_map(plugin: Any) -> dict[str, dict]:
    """Build a host:port -> {rxb, txb} map from Reticulum interface stats.

    Interface names in shared-instance mode look like:
        TCPInterface[TCP Client label/host:port]
    We parse the host:port from after the '/' and strip the trailing ']'.
    """
    traffic_map: dict[str, dict] = {}
    try:
        rns_instance = getattr(plugin.app, "reticulum", None)
        if not rns_instance or not hasattr(rns_instance, "get_interface_stats"):
            return traffic_map
        stats = rns_instance.get_interface_stats()
        for entry in stats.get("interfaces", []):
            if "TCPClient" not in entry.get("type", ""):
                continue
            traffic = {"rxb": entry.get("rxb", 0), "txb": entry.get("txb", 0)}
            name = entry.get("name", "")
            if "/" in name:
                # "TCPInterface[TCP Client label/host:port]" -> "host:port"
                addr = name.split("/", 1)[1].rstrip("]")
                traffic_map[addr] = traffic
            ip = entry.get("target_ip")
            port = entry.get("target_port")
            if ip and port:
                traffic_map[f"{ip}:{port}"] = traffic
    except Exception:
        pass
    return traffic_map


def _ok(data: Any) -> aiohttp.web.Response:
    """Return a success JSON response."""
    import json

    body = json.dumps({"ok": True, "data": data, "timestamp": time.time()})
    return aiohttp.web.Response(text=body, content_type="application/json")


def _error(message: str, status: int = 400) -> aiohttp.web.Response:
    """Return an error JSON response."""
    import json

    body = json.dumps({"ok": False, "error": message, "code": status})
    return aiohttp.web.Response(
        text=body, status=status, content_type="application/json"
    )


def _get_plugin(request: aiohttp.web.Request):
    """Get the WebDashboardPlugin from the request's app."""
    return request.app["plugin"]


# ── Route registration hub ───────────────────────────────────────────


def setup_api_routes(app: aiohttp.web.Application) -> None:
    """Register all API routes on the aiohttp application."""
    from reticulumpi.builtin_plugins.web_dashboard.api_interfaces import (
        setup_interface_routes,
    )
    from reticulumpi.builtin_plugins.web_dashboard.api_mesh import setup_mesh_routes
    from reticulumpi.builtin_plugins.web_dashboard.api_services import (
        setup_service_routes,
    )

    # Version
    app.router.add_get("/api/version", handle_version)
    # Auth
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/auth/login", handle_form_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    # System
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/node", handle_node)
    app.router.add_get("/api/metrics", handle_metrics)
    app.router.add_get("/api/plugins", handle_plugins)
    app.router.add_get("/api/plugins/{name}", handle_plugin_detail)
    app.router.add_post("/api/services/restart", handle_services_restart)
    app.router.add_get("/api/config", handle_config)
    # Spectrum presets
    app.router.add_get("/api/spectrum/presets", handle_spectrum_presets)
    app.router.add_post("/api/spectrum/preset", handle_spectrum_switch_preset)
    # Chirp capture / detector
    app.router.add_post("/api/chirp/capture", handle_chirp_capture)
    app.router.add_get("/api/chirp/status", handle_chirp_status)
    app.router.add_get("/api/chirp/history", handle_chirp_history)
    app.router.add_post("/api/chirp/waterfall", handle_chirp_waterfall_toggle)
    # Domain sub-modules
    setup_interface_routes(app)
    setup_mesh_routes(app)
    setup_service_routes(app)


# ── Version endpoint ─────────────────────────────────────────────────


async def handle_version(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/version — API version and compatibility info."""
    plugin = _get_plugin(request)
    return _ok({
        "api_version": API_VERSION,
        "app_version": plugin.app._get_version(),
    })


# ── Auth endpoints ───────────────────────────────────────────────────


async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/auth/login — authenticate and receive session token."""
    plugin = _get_plugin(request)
    auth = plugin._auth
    remote_ip = request.remote or "unknown"

    if auth.is_rate_limited(remote_ip):
        retry_after = auth.get_retry_after(remote_ip)
        resp = _error("Too many login attempts", 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return _error("Invalid request body", 400)

    if not password:
        return _error("Password is required", 400)
    if len(password) > 256:
        return _error("Password too long", 400)

    token = auth.login(password, remote_ip)
    if not token:
        return _error("Invalid password", 401)

    resp = _ok({"token": token})

    # Set session cookie
    ssl_config = plugin.config.get("ssl", {})
    secure = ssl_config.get("enabled", False)
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        secure=secure,
        samesite="Lax",
        max_age=int(auth.session_timeout),
        path="/",
    )
    return resp


async def handle_form_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /auth/login — form-based login that redirects with Set-Cookie.

    Browsers reliably store cookies from form POST redirects, unlike fetch().
    """
    plugin = _get_plugin(request)
    auth = plugin._auth
    remote_ip = request.remote or "unknown"

    if auth.is_rate_limited(remote_ip):
        raise aiohttp.web.HTTPFound("/login.html?error=rate_limited")

    try:
        data = await request.post()
        password = data.get("password", "")
    except Exception:
        raise aiohttp.web.HTTPFound("/login.html?error=invalid")

    if not password:
        raise aiohttp.web.HTTPFound("/login.html?error=empty")
    if len(password) > 256:
        raise aiohttp.web.HTTPFound("/login.html?error=too_long")

    token = auth.login(password, remote_ip)
    if not token:
        raise aiohttp.web.HTTPFound("/login.html?error=invalid")

    ssl_config = plugin.config.get("ssl", {})
    secure = ssl_config.get("enabled", False)

    resp = aiohttp.web.HTTPFound("/")
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        secure=secure,
        samesite="Lax",
        max_age=int(auth.session_timeout),
        path="/",
    )
    raise resp


async def handle_logout(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/auth/logout — invalidate current session."""
    plugin = _get_plugin(request)
    # Extract token from Authorization header or session cookie
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get("session", "")
    if token:
        plugin._auth.logout(token)

    resp = _ok({"message": "Logged out"})
    resp.del_cookie("session", path="/")
    return resp


# ── System endpoints ─────────────────────────────────────────────────


async def handle_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/status — full app status."""
    plugin = _get_plugin(request)
    status = plugin.app.get_status()
    return _ok(status)


async def handle_node(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/node — node identity and info."""
    import RNS

    plugin = _get_plugin(request)
    app = plugin.app

    identity_hash = ""
    if app.identity:
        identity_hash = RNS.prettyhexrep(app.identity.hash)

    data = {
        "node_name": app.node_name,
        "identity_hash": identity_hash,
        "version": app._get_version(),
        "uptime": time.time() - plugin._start_time if plugin._active else 0,
    }
    return _ok(data)


async def handle_metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/metrics — latest system_monitor metrics."""
    plugin = _get_plugin(request)
    monitor = plugin.app.get_plugin("system_monitor")

    if monitor and hasattr(monitor, "latest_metrics"):
        return _ok(monitor.latest_metrics)

    return _ok({"message": "system_monitor plugin not available", "metrics": {}})


async def handle_plugins(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins — all plugins with statuses."""
    plugin = _get_plugin(request)
    app = plugin.app

    plugins_data = {}
    for name, p in app.plugins.items():
        try:
            status = p.get_status()
        except Exception:
            status = {"error": "status collection failed"}
        plugins_data[name] = {
            "name": name,
            "version": p.plugin_version,
            "description": p.plugin_description,
            "status": status,
            "address": _get_plugin_address(p),
        }

    failed = [
        {"name": name, "error": reason} for name, reason in app._failed_plugins
    ]

    return _ok({"plugins": plugins_data, "failed_plugins": failed})


async def handle_plugin_detail(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/plugins/{name} — single plugin detail."""
    plugin = _get_plugin(request)
    name = request.match_info["name"]
    p = plugin.app.get_plugin(name)

    if not p:
        return _error(f"Plugin '{name}' not found", 404)

    try:
        status = p.get_status()
    except Exception:
        status = {"error": "status collection failed"}

    return _ok({
        "name": name,
        "version": p.plugin_version,
        "description": p.plugin_description,
        "status": status,
        "address": _get_plugin_address(p),
    })


_last_restart_time: float = 0.0
_RESTART_COOLDOWN = 60.0


async def handle_services_restart(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/services/restart — restart rnsd + reticulumpi."""
    import asyncio

    global _last_restart_time

    if not request.get("token"):
        return _error("Authentication required", 401)

    now = time.monotonic()
    if now - _last_restart_time < _RESTART_COOLDOWN:
        return _error("Service restart already in progress", 429)
    _last_restart_time = now

    async def _do_restart() -> None:
        await asyncio.sleep(2)  # let HTTP response flush
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "rnsd",
        )
        await proc.wait()
        await asyncio.sleep(3)  # rnsd startup time
        await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "reticulumpi",
        )

    asyncio.create_task(_do_restart())
    return _ok({"message": "Restarting services..."})


async def handle_spectrum_presets(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/spectrum/presets — list available frequency presets."""
    plugin = _get_plugin(request)
    scanner = plugin.app.plugins.get("spectrum_scanner")
    if not scanner or not hasattr(scanner, "get_presets"):
        return _error("spectrum_scanner plugin not enabled", 404)
    return _ok(scanner.get_presets())


async def handle_spectrum_switch_preset(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/spectrum/preset — switch the active frequency preset."""
    if not request.get("token"):
        return _error("Authentication required", 401)

    plugin = _get_plugin(request)
    scanner = plugin.app.plugins.get("spectrum_scanner")
    if not scanner or not hasattr(scanner, "switch_preset"):
        return _error("spectrum_scanner plugin not enabled", 404)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    preset_name = body.get("preset")
    if not preset_name:
        return _error("'preset' field required", 400)

    try:
        result = scanner.switch_preset(preset_name)
        return _ok(result)
    except ValueError as exc:
        return _error(str(exc), 400)


def _get_chirp_plugin(request: aiohttp.web.Request):
    plugin = _get_plugin(request)
    return (
        plugin.app.plugins.get("chirp_detector")
        or plugin.app.plugins.get("lora_chirp_viewer")
    )


async def handle_chirp_capture(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/chirp/capture — trigger an on-demand chirp spectrogram capture."""
    if not request.get("token"):
        return _error("Authentication required", 401)

    viewer = _get_chirp_plugin(request)
    if not viewer:
        return _error("chirp plugin not enabled", 404)

    if viewer.plugin_name == "chirp_detector":
        return _error(
            "chirp_detector uses continuous detection — "
            "on-demand capture is not supported; use the streaming waterfall instead",
            409,
        )

    if not hasattr(viewer, "capture_chirps"):
        return _error("chirp plugin does not support on-demand capture", 404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    freq_hz = int(float(body.get("freq_mhz", 0)) * 1e6) or None
    sample_rate = int(body.get("sample_rate", 0)) or None
    duration_s = float(body.get("duration_s", 0)) or None

    try:
        capture_id = viewer.capture_chirps(freq_hz, sample_rate, duration_s)
    except RuntimeError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)

    return _ok({"status": "capturing", "capture_id": capture_id})


async def handle_chirp_status(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/chirp/status — current capture state."""
    viewer = _get_chirp_plugin(request)
    if not viewer:
        return _error("chirp plugin not enabled", 404)
    status_fn = (
        getattr(viewer, "get_capture_status", None)
        or getattr(viewer, "get_snapshot", None)
    )
    if not status_fn:
        return _error("chirp plugin has no status method", 404)
    return _ok(status_fn())


async def handle_chirp_history(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """GET /api/chirp/history — chirp waterfall buffer."""
    viewer = _get_chirp_plugin(request)
    hist_fn = (
        getattr(viewer, "get_waterfall_history", None)
        or getattr(viewer, "get_chirp_waterfall_history", None)
    ) if viewer else None
    if not hist_fn:
        return _error("chirp plugin not enabled", 404)
    return _ok(hist_fn())


async def handle_chirp_waterfall_toggle(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    """POST /api/chirp/waterfall — toggle chirp waterfall on/off."""
    if not request.get("token"):
        return _error("Authentication required", 401)

    plugin = _get_plugin(request)
    detector = plugin.app.plugins.get("chirp_detector")
    if not detector or not hasattr(detector, "set_waterfall_enabled"):
        return _error("chirp_detector plugin not enabled", 404)

    try:
        body = await request.json()
    except Exception:
        return _error("JSON body required", 400)

    enabled = bool(body.get("enabled", False))
    detector.set_waterfall_enabled(enabled)
    return _ok({"waterfall_enabled": detector._waterfall_enabled})


async def handle_config(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/config — read-only, sanitized config view."""
    plugin = _get_plugin(request)
    config = plugin.app.config

    # Build sanitized plugin config
    plugins_config = {}
    for name, cfg in config.plugins.items():
        plugins_config[name] = {
            k: v for k, v in cfg.items() if k not in SENSITIVE_KEYS
        }

    data = {
        "node_name": config.node_name,
        "log_level": config.log_level,
        "use_shared_instance": config.use_shared_instance,
        "plugin_paths": config.plugin_paths,
        "plugins": plugins_config,
    }
    return _ok(data)
