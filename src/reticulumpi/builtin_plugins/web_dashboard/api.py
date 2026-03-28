"""JSON API route handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import aiohttp.web

if TYPE_CHECKING:
    pass

SENSITIVE_KEYS = frozenset({"password", "password_hash"})


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


def setup_api_routes(app: aiohttp.web.Application) -> None:
    """Register all API routes on the aiohttp application."""
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/auth/login", handle_form_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/node", handle_node)
    app.router.add_get("/api/metrics", handle_metrics)
    app.router.add_get("/api/plugins", handle_plugins)
    app.router.add_get("/api/plugins/{name}", handle_plugin_detail)
    app.router.add_get("/api/interfaces", handle_interfaces)
    app.router.add_get("/api/config", handle_config)
    # Mesh awareness endpoints
    app.router.add_get("/api/mesh/nodes", handle_mesh_nodes)
    app.router.add_get("/api/mesh/telemetry", handle_mesh_telemetry)
    app.router.add_get("/api/alerts", handle_alerts)
    app.router.add_get("/api/files", handle_files)
    app.router.add_get("/api/sensors", handle_sensors)
    app.router.add_get("/api/emergency", handle_emergency)


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


# --- Auth endpoints ---


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


# --- Data endpoints ---


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


async def handle_interfaces(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/interfaces — active RNS network interfaces."""
    import RNS

    interfaces = []
    try:
        for iface in RNS.Transport.interfaces:
            info: dict[str, Any] = {
                "name": str(iface),
                "type": iface.__class__.__name__,
                "online": getattr(iface, "online", None),
            }
            if hasattr(iface, "bitrate"):
                info["bitrate"] = iface.bitrate
            if hasattr(iface, "peers"):
                info["peers"] = len(iface.peers) if iface.peers else 0
            if hasattr(iface, "IN") and hasattr(iface, "OUT"):
                info["direction"] = "bidirectional"
            if hasattr(iface, "rxb"):
                info["rxb"] = iface.rxb
            if hasattr(iface, "txb"):
                info["txb"] = iface.txb
            interfaces.append(info)
    except Exception as exc:
        return _ok({"interfaces": interfaces, "error": f"Partial collection: {exc}"})

    return _ok({"interfaces": interfaces})


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


# --- Mesh awareness endpoints ---


async def handle_mesh_nodes(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/nodes — known nodes from network_map plugin."""
    plugin = _get_plugin(request)
    network_map = plugin.app.get_plugin("network_map")
    if not network_map or not hasattr(network_map, "get_known_nodes"):
        return _ok({"nodes": [], "message": "network_map plugin not available"})
    return _ok({"nodes": network_map.get_known_nodes()})


async def handle_mesh_telemetry(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/mesh/telemetry — peer metrics from mesh_telemetry plugin."""
    plugin = _get_plugin(request)
    telemetry = plugin.app.get_plugin("mesh_telemetry")
    if not telemetry or not hasattr(telemetry, "get_peer_metrics"):
        return _ok({"peers": [], "message": "mesh_telemetry plugin not available"})
    return _ok({"peers": telemetry.get_peer_metrics()})


async def handle_alerts(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/alerts — alert system status."""
    plugin = _get_plugin(request)
    alert_sys = plugin.app.get_plugin("alert_system")
    if not alert_sys:
        return _ok({"status": None, "message": "alert_system plugin not available"})
    try:
        status = alert_sys.get_status()
    except Exception:
        status = {"error": "status collection failed"}
    return _ok(status)


async def handle_files(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/files — shared files from file_transfer plugin."""
    plugin = _get_plugin(request)
    ft = plugin.app.get_plugin("file_transfer")
    if not ft or not hasattr(ft, "get_shared_files"):
        return _ok({"files": [], "message": "file_transfer plugin not available"})
    return _ok({"files": ft.get_shared_files()})


async def handle_sensors(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/sensors — latest sensor readings from sensor_framework plugin."""
    plugin = _get_plugin(request)
    sf = plugin.app.get_plugin("sensor_framework")
    if not sf or not hasattr(sf, "get_latest_readings"):
        return _ok({"sensors": {}, "message": "sensor_framework plugin not available"})
    return _ok({"sensors": sf.get_latest_readings()})


async def handle_emergency(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/emergency — recent emergency broadcast messages."""
    plugin = _get_plugin(request)
    eb = plugin.app.get_plugin("emergency_broadcast")
    if not eb or not hasattr(eb, "get_messages"):
        return _ok({"messages": [], "message": "emergency_broadcast plugin not available"})
    return _ok({"messages": eb.get_messages(), "status": eb.get_status()})
